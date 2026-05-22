"""Storage backend abstraction.

Single API for SQLite (default, single-file) and Postgres (opt-in).
Anything else in Solomon talks through ``get_conn()`` and parameterised
SQL — never through psycopg or sqlite3 directly.

The two backends speak almost the same dialect. Differences:
  - Param style: SQLite uses '?', Postgres uses '%s'. We use '?' in our
    code and translate at execute time when running on Postgres.
  - JSONB: Postgres has it native, SQLite stores TEXT and we json.loads()
    on read. Helper functions hide the difference.
  - Vectors: pgvector on Postgres, sqlite-vec on SQLite. Same conceptual
    interface (nearest-neighbour search by cosine distance).
  - Autoincrement PK: Postgres BIGSERIAL, SQLite INTEGER PRIMARY KEY
    AUTOINCREMENT. Schema file uses both with a small substitution.

Selection: SOLOMON_DB_URL=sqlite:///path  (default if unset)
           SOLOMON_DB_URL=postgresql://...

For the default install (no env var set), Solomon uses
``~/.hermes/solomon/solomon.db``.

This module is the only place that imports psycopg or sqlite3. The
rest of Solomon talks to ``get_conn()`` and the helpers here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Optional, Tuple

logger = logging.getLogger("solomon.storage")

# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

_BACKEND: Optional[str] = None  # 'sqlite' | 'postgres'
_DSN: Optional[str] = None
_INIT_LOCK = threading.Lock()
_PG_POOL = None  # psycopg pool, lazy-loaded


def _resolve_dsn() -> Tuple[str, str]:
    """Decide which backend to use. Returns (backend_name, dsn)."""
    raw = os.getenv("SOLOMON_DB_URL", "").strip()
    if not raw:
        # Default: SQLite at ~/.hermes/solomon/solomon.db
        home = Path(os.path.expanduser(os.getenv("HERMES_HOME", "~/.hermes")))
        path = home / "solomon" / "solomon.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return "sqlite", f"sqlite:///{path}"
    if raw.startswith(("postgres://", "postgresql://")):
        return "postgres", raw
    if raw.startswith("sqlite://"):
        return "sqlite", raw
    # Bare path → assume sqlite
    return "sqlite", f"sqlite:///{raw}"


def backend() -> str:
    """Return 'sqlite' or 'postgres'. Initializes on first call."""
    global _BACKEND, _DSN
    if _BACKEND is None:
        with _INIT_LOCK:
            if _BACKEND is None:
                _BACKEND, _DSN = _resolve_dsn()
                logger.info("Solomon storage backend: %s", _BACKEND)
    return _BACKEND


def dsn() -> str:
    backend()  # ensures init
    assert _DSN is not None
    return _DSN


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

@contextmanager
def get_conn() -> Iterator[Any]:
    """Yield a connection. Caller must commit explicitly.

    Usage:
        with get_conn() as conn:
            with cursor(conn) as cur:
                cur.execute(...)
            conn.commit()
    """
    if backend() == "sqlite":
        path = dsn().replace("sqlite:///", "")
        conn = sqlite3.connect(path, isolation_level="DEFERRED", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            # WAL mode = readers don't block writers, writers don't block readers.
            # Synchronous=NORMAL is the safe-and-fast setting for WAL.
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            yield conn
        finally:
            conn.close()
    else:
        global _PG_POOL
        if _PG_POOL is None:
            try:
                import psycopg  # noqa: F401
                from psycopg_pool import ConnectionPool
            except ImportError as e:
                raise RuntimeError(
                    "Postgres backend requested but psycopg is not installed. "
                    "Run `pip install 'psycopg[binary,pool]' pgvector`, or unset "
                    "SOLOMON_DB_URL to use the SQLite default."
                ) from e
            _PG_POOL = ConnectionPool(conninfo=dsn(), min_size=1, max_size=10, open=True)
        with _PG_POOL.connection() as conn:
            yield conn


@contextmanager
def cursor(conn: Any) -> Iterator[Any]:
    """Yield a cursor that auto-closes."""
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# Param-style translation
# ---------------------------------------------------------------------------

_PG_PARAM_RE = re.compile(r"\?")


def execute(cur: Any, sql: str, params: Iterable[Any] = ()) -> Any:
    """Execute a parameterised query. Always use '?' as the placeholder
    in your SQL; this function rewrites to '%s' when running on Postgres.

    This is the canonical way to run a query through the storage layer.
    """
    if backend() == "postgres":
        sql = _PG_PARAM_RE.sub("%s", sql)
    return cur.execute(sql, tuple(params))


def executemany(cur: Any, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> Any:
    if backend() == "postgres":
        sql = _PG_PARAM_RE.sub("%s", sql)
    return cur.executemany(sql, list(seq_of_params))


def lastrowid(cur: Any) -> Optional[int]:
    """Return the last inserted PK. SQLite has .lastrowid; Postgres needs
    a RETURNING clause and fetchone — call this AFTER you've fetched the
    returning row, or use insert_returning() below.
    """
    return getattr(cur, "lastrowid", None)


def insert_returning(conn: Any, sql: str, params: Iterable[Any]) -> Any:
    """Run an INSERT and return the PK.

    Pattern: pass `sql` with a RETURNING clause for Postgres (e.g.
    "INSERT INTO foo(...) VALUES (?,?,?) RETURNING foo_id"). On SQLite,
    we strip the RETURNING and use ``lastrowid``.
    """
    if backend() == "postgres":
        with cursor(conn) as cur:
            execute(cur, sql, params)
            row = cur.fetchone()
            return row[0] if row else None
    # SQLite: strip the RETURNING clause
    stripped = re.sub(r"\s+RETURNING\s+\w+\s*$", "", sql, flags=re.IGNORECASE).rstrip()
    with cursor(conn) as cur:
        execute(cur, stripped, params)
        return cur.lastrowid


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def jsonify(value: Any) -> str:
    """Convert a Python value into a string for storage in a JSON/JSONB
    column. Works identically on both backends.
    """
    if value is None:
        return "null"
    return json.dumps(value, default=str)


def parse_json(value: Any) -> Any:
    """Parse a JSON-typed column value back into Python. On SQLite this
    is always a string; on Postgres psycopg already deserialises it.
    """
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except Exception:  # noqa: BLE001
            return None
    return value


def row_to_dict(row: Any) -> dict:
    """Convert a row (sqlite3.Row, tuple, or psycopg row) to a plain dict."""
    if hasattr(row, "keys"):  # sqlite3.Row
        return {k: row[k] for k in row.keys()}
    return dict(row) if isinstance(row, dict) else {}


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

_SCHEMA_CACHE: dict = {}


def _load_schema_for(backend_name: str) -> str:
    if backend_name in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[backend_name]
    here = Path(__file__).parent
    path = here / f"schema_{backend_name}.sql"
    if not path.exists():
        # Fallback to generic schema.sql + simple substitution.
        path = here / "schema.sql"
    txt = path.read_text(encoding="utf-8")
    _SCHEMA_CACHE[backend_name] = txt
    return txt


def init_storage(adapter: Any = None) -> None:  # noqa: ANN401  (Hermes adapter or None)
    """Initialise the configured backend. Idempotent.

    Solomon refuses to start if this fails — running without storage
    would silently lose the data this whole system exists to collect.
    """
    bk = backend()
    logger.info("Initialising Solomon storage (%s)...", bk)
    schema = _load_schema_for(bk)
    with get_conn() as conn:
        if bk == "sqlite":
            # Apply the schema. SQLite has executescript() for multi-statement
            # files; the schema file uses CREATE TABLE IF NOT EXISTS so it's
            # idempotent.
            conn.executescript(schema)
            conn.commit()
        else:
            with cursor(conn) as cur:
                cur.execute(schema)
            conn.commit()
    logger.info("Solomon storage ready (%s).", bk)


# Backwards-compat shim — older code in the repo calls ``get_pool()``.
def get_pool():  # noqa: ANN201
    """Return a connection-pool-like object. Provided for compatibility
    with older Solomon code; new code should use ``get_conn()`` directly.
    """
    class _Pool:
        def connection(self):
            return get_conn()
    return _Pool()

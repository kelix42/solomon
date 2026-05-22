"""Postgres connection pool + schema bootstrap.

Solomon refuses to start without a working database. Running without the
decision log would silently lose the data this whole system exists to
collect — worse than not running at all.

Two backends supported via the SOLOMON_DATABASE_URL env var:
  - postgresql://...  (local Docker, bare-metal, or self-hosted)
  - postgresql://... pointed at Supabase (managed)

Both speak the same SQL. The schema is the same.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("solomon.storage")

# We lazy-import psycopg so this module can be imported even on machines
# that haven't installed the deps yet (e.g. during `pip install solomon-brain`).
_pool = None
_SCHEMA_SQL: Optional[str] = None


def _schema_sql() -> str:
    global _SCHEMA_SQL
    if _SCHEMA_SQL is None:
        path = Path(__file__).parent / "schema.sql"
        _SCHEMA_SQL = path.read_text(encoding="utf-8")
    return _SCHEMA_SQL


def get_database_url() -> str:
    """Return the database URL, with a sensible default for local dev."""
    url = os.getenv("SOLOMON_DATABASE_URL")
    if url:
        return url
    # Default to local Postgres on the standard port. The installer will
    # have set this up (or pointed at a Supabase URL) before we get here.
    return "postgresql://solomon:solomon@localhost:5432/solomon"


def init_storage(adapter) -> None:  # noqa: ANN001
    """Initialize the pool and ensure the schema is present.

    Called once at plugin register time. If anything goes wrong, raises;
    the caller (plugin.register) catches and refuses to start Solomon.
    """
    global _pool
    try:
        import psycopg  # noqa: F401
        from psycopg_pool import ConnectionPool
    except ImportError as e:
        raise RuntimeError(
            "psycopg / psycopg_pool not installed. Run `pip install solomon-brain` "
            "or `pip install 'psycopg[binary,pool]' pgvector`."
        ) from e

    url = get_database_url()
    logger.info("Solomon connecting to database (host hidden).")
    _pool = ConnectionPool(conninfo=url, min_size=1, max_size=10, open=True)

    # Smoke test + schema bootstrap.
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()
            # Apply schema. CREATE ... IF NOT EXISTS makes this idempotent.
            cur.execute(_schema_sql())
        conn.commit()
    logger.info("Solomon storage ready.")


def get_pool():
    """Return the connection pool. Raises if init_storage wasn't called."""
    if _pool is None:
        raise RuntimeError(
            "Solomon storage pool not initialized. Plugin registration likely failed; "
            "check ~/.hermes/logs/agent.log."
        )
    return _pool


def with_connection(fn):  # noqa: ANN001
    """Decorator: pass a Postgres connection as the first argument of fn."""
    def wrapper(*args, **kwargs):
        pool = get_pool()
        with pool.connection() as conn:
            return fn(conn, *args, **kwargs)
    return wrapper

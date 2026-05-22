"""SHA256-keyed ingest manifest — the ``ingested_files`` table.

Per REPORT-CORPUS.md §1.6 + §4.5 Phase A step 5: re-ingesting a file with
the same SHA256 is a no-op. This module is the single source of truth for
what has been processed.

Ported from /root/projects/solomon-from-drive/corpus_ingest/manifest.py
with three changes:

1. **Pool API** — uses ``solomon.storage.pool`` (``get_conn`` /
   ``execute`` / ``jsonify``) instead of raw sqlite3. SQL uses ``?``
   placeholders so Postgres works too.
2. **tenant_id** — every row is tagged with the active tenant.
3. **ULID-style IDs** — same hex32 shape but generated with ``uuid4().hex``
   so we don't need the ``ulid-py`` dep at import time.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..storage.pool import cursor, execute, get_conn, jsonify, parse_json, row_to_dict

logger = logging.getLogger("solomon.corpus.manifest")

# ---------------------------------------------------------------------------
# Status enum mirror — matches the CHECK constraint in schema.sql when present.
# SQLite stores TEXT so we enforce these in Python.
# ---------------------------------------------------------------------------

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_FORGOTTEN = "forgotten"

VALID_STATUSES = {
    STATUS_PENDING,
    STATUS_IN_PROGRESS,
    STATUS_SUCCESS,
    STATUS_PARTIAL,
    STATUS_FAILED,
    STATUS_FORGOTTEN,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _new_id() -> str:
    return uuid.uuid4().hex


def _default_tenant() -> str:
    """Match the resolution Solomon uses elsewhere. The seed in
    schema.sql installs 'default'; callers can override via env.
    """
    return os.getenv("SOLOMON_TENANT_ID", "default")


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def file_sha256(path: Path, chunk_size: int = 1 << 16) -> str:
    """Stream a file through SHA-256. Identical bytes -> identical digest."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(chunk_size), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Read-side
# ---------------------------------------------------------------------------


def existing_for_sha(sha: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the most recent ingested_files row for this SHA, or None.

    The ``sha256`` column is UNIQUE so at most one row ever matches.
    """
    tid = tenant_id or _default_tenant()
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT id, tenant_id, sha256, status, raw_path, "
                "       inbox_path_at_ingest, category, size_bytes, "
                "       vector_count, wiki_pages_touched, error_message, "
                "       ingested_at "
                "FROM ingested_files WHERE sha256 = ? AND tenant_id = ?",
                (sha, tid),
            )
            row = cur.fetchone()
    if not row:
        return None
    d = row_to_dict(row) if hasattr(row, "keys") else dict(
        zip(
            [
                "id",
                "tenant_id",
                "sha256",
                "status",
                "raw_path",
                "inbox_path_at_ingest",
                "category",
                "size_bytes",
                "vector_count",
                "wiki_pages_touched",
                "error_message",
                "ingested_at",
            ],
            row,
        )
    )
    d["wiki_pages_touched"] = parse_json(d.get("wiki_pages_touched")) or []
    return d


def is_already_ingested(sha: str, tenant_id: Optional[str] = None) -> bool:
    """True if the SHA exists with status=success. Re-ingest is a no-op."""
    row = existing_for_sha(sha, tenant_id=tenant_id)
    return bool(row and row.get("status") == STATUS_SUCCESS)


def list_by_status(
    status: str,
    *,
    tenant_id: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Return rows with the given status, newest first."""
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    tid = tenant_id or _default_tenant()
    out: List[Dict[str, Any]] = []
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT id, sha256, status, raw_path, category, size_bytes, "
                "       vector_count, wiki_pages_touched, error_message, "
                "       ingested_at "
                "FROM ingested_files "
                "WHERE tenant_id = ? AND status = ? "
                "ORDER BY ingested_at DESC "
                "LIMIT ?",
                (tid, status, limit),
            )
            rows = cur.fetchall()
    for r in rows:
        d = row_to_dict(r) if hasattr(r, "keys") else dict(
            zip(
                [
                    "id",
                    "sha256",
                    "status",
                    "raw_path",
                    "category",
                    "size_bytes",
                    "vector_count",
                    "wiki_pages_touched",
                    "error_message",
                    "ingested_at",
                ],
                r,
            )
        )
        d["wiki_pages_touched"] = parse_json(d.get("wiki_pages_touched")) or []
        out.append(d)
    return out


def stats(tenant_id: Optional[str] = None) -> Dict[str, int]:
    """Quick counters for the ``solomon corpus stats`` CLI subcommand."""
    tid = tenant_id or _default_tenant()
    out = {s: 0 for s in VALID_STATUSES}
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT status, COUNT(*) FROM ingested_files "
                "WHERE tenant_id = ? GROUP BY status",
                (tid,),
            )
            for row in cur.fetchall():
                status = row[0]
                count = int(row[1])
                if status in out:
                    out[status] = count
    out["total"] = sum(v for k, v in out.items() if k != "total")
    return out


# ---------------------------------------------------------------------------
# Write-side
# ---------------------------------------------------------------------------


def insert_pending(
    *,
    sha: str,
    inbox_path: str,
    size_bytes: int,
    category: str,
    tenant_id: Optional[str] = None,
) -> str:
    """Create a 'pending' row before doing any work. Returns the row id.

    Idempotent on SHA: if a row already exists for this SHA, we return
    its id instead of creating a duplicate. That way a retry after a
    crash hits the same row.
    """
    tid = tenant_id or _default_tenant()
    existing = existing_for_sha(sha, tenant_id=tid)
    if existing:
        return existing["id"]
    row_id = _new_id()
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO ingested_files "
                "(id, tenant_id, sha256, inbox_path_at_ingest, size_bytes, "
                " category, status, ingested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row_id,
                    tid,
                    sha,
                    inbox_path,
                    size_bytes,
                    category,
                    STATUS_PENDING,
                    _now(),
                ),
            )
        conn.commit()
    return row_id


def _update_status(
    row_id: str,
    status: str,
    *,
    raw_path: Optional[str] = None,
    vector_count: Optional[int] = None,
    wiki_pages_touched: Optional[List[str]] = None,
    error: Optional[str] = None,
) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    sets: List[str] = ["status = ?"]
    params: List[Any] = [status]
    if raw_path is not None:
        sets.append("raw_path = ?")
        params.append(raw_path)
    if vector_count is not None:
        sets.append("vector_count = ?")
        params.append(int(vector_count))
    if wiki_pages_touched is not None:
        sets.append("wiki_pages_touched = ?")
        params.append(jsonify(wiki_pages_touched))
    if error is not None:
        sets.append("error_message = ?")
        params.append(error[:1000])
    params.append(row_id)
    sql = f"UPDATE ingested_files SET {', '.join(sets)} WHERE id = ?"
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, sql, params)
        conn.commit()


def mark_in_progress(row_id: str) -> None:
    _update_status(row_id, STATUS_IN_PROGRESS)


def mark_success(
    row_id: str,
    raw_path: str,
    vector_count: int,
    wiki_pages_touched: Optional[List[str]] = None,
) -> None:
    _update_status(
        row_id,
        STATUS_SUCCESS,
        raw_path=raw_path,
        vector_count=vector_count,
        wiki_pages_touched=wiki_pages_touched or [],
    )


def mark_partial(
    row_id: str,
    raw_path: Optional[str],
    vector_count: int,
    error: str,
) -> None:
    _update_status(
        row_id,
        STATUS_PARTIAL,
        raw_path=raw_path,
        vector_count=vector_count,
        error=error,
    )


def mark_failed(row_id: str, error: str) -> None:
    _update_status(row_id, STATUS_FAILED, error=error)


def mark_forgotten(row_id: str) -> None:
    """Owner-initiated deletion (corpus_forget cascade)."""
    _update_status(row_id, STATUS_FORGOTTEN)

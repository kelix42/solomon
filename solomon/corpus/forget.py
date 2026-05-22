"""Owner-initiated forget cascade for corpus content.

REPORT-CORPUS.md §1.12 (forget cascade). When the owner asks Solomon to
forget a file, we cascade the deletion deterministically — no LLM:

  1. Resolve the file by SHA256, raw_path, or ingested_files id.
  2. Delete the raw bytes on disk (corpus/raw/<category>/<file>).
  3. Delete all ``corpus_raw`` embeddings rows whose metadata.raw_path
     matches the deleted file.
  4. Delete queued ``proposed_rules`` derived from this source_path,
     and any paired ``mentoring_queue`` rows.
  5. Mark the ``ingested_files`` row as ``forgotten``.

What this DOES NOT touch (deferred per Drive design):
  - Wiki pages that synthesised facts from this source — wiki pages
    pool content from many sources, so removing them is a higher-order
    operation that needs an LLM rewrite pass. The lint job surfaces
    pages with dangling source citations to the mentoring queue, and
    the owner decides per-page.
  - ``captured_items`` rows promoted from proposed_rules — those have
    survived owner review and are now part of the brain proper.

The ``encrypt_to_pre_redaction`` quarantine flow in Drive's install.sh
is out of scope for this module; the disk delete here is unconditional.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import embed as corpus_embed
from . import manifest as cm
from . import rules as cr
from .schema_config import corpus_root
from ..storage.pool import cursor, execute, get_conn, parse_json

logger = logging.getLogger("solomon.corpus.forget")


def _default_tenant() -> str:
    return os.getenv("SOLOMON_TENANT_ID", "default")


def _resolve_raw_path(rel: str) -> Path:
    cr_root = corpus_root()
    if rel.startswith("corpus/"):
        return cr_root.parent / rel
    return cr_root / rel


def _embeddings_for_raw_path(
    raw_path: str, *, tenant_id: str
) -> List[str]:
    """Return source_ids of corpus_raw embeddings that reference raw_path."""
    out: List[str] = []
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT source_id, metadata FROM embeddings "
                "WHERE tenant_id = ? AND source_table = ? AND metadata LIKE ?",
                (
                    tenant_id,
                    corpus_embed.SOURCE_TABLE_CORPUS_RAW,
                    f"%{raw_path}%",
                ),
            )
            rows = cur.fetchall()
    for row in rows:
        meta = parse_json(row[1]) or {}
        if meta.get("raw_path") == raw_path:
            out.append(row[0])
    return out


def forget_file(
    *,
    sha256: Optional[str] = None,
    raw_path: Optional[str] = None,
    file_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    delete_disk: bool = True,
) -> Dict[str, Any]:
    """Cascade-forget a corpus file. Provide one of sha256, raw_path, or file_id.

    Returns a summary dict:
      {
        "found": bool,
        "file_id": str | None,
        "raw_path": str | None,
        "embeddings_deleted": int,
        "rules_deleted": int,
        "disk_deleted": bool,
      }
    """
    tid = tenant_id or _default_tenant()
    summary: Dict[str, Any] = {
        "found": False,
        "file_id": None,
        "raw_path": None,
        "embeddings_deleted": 0,
        "rules_deleted": 0,
        "disk_deleted": False,
    }

    row = _locate_row(sha256=sha256, raw_path=raw_path, file_id=file_id, tenant_id=tid)
    if not row:
        return summary

    summary["found"] = True
    summary["file_id"] = row["id"]
    summary["raw_path"] = row.get("raw_path")
    rp = row.get("raw_path") or ""

    # 1. Disk delete.
    if delete_disk and rp:
        try:
            path = _resolve_raw_path(rp)
            if path.exists():
                path.unlink()
                summary["disk_deleted"] = True
        except Exception:  # noqa: BLE001
            logger.exception("forget: could not unlink %s", rp)

    # 2. Embeddings cascade.
    if rp:
        try:
            ids = _embeddings_for_raw_path(rp, tenant_id=tid)
            if ids:
                summary["embeddings_deleted"] = corpus_embed.delete_by_source_ids(
                    corpus_embed.SOURCE_TABLE_CORPUS_RAW, ids, tenant_id=tid
                )
        except Exception:  # noqa: BLE001
            logger.exception("forget: embeddings cleanup failed for %s", rp)

    # 3. proposed_rules + mentoring_queue cascade (only when we know rp).
    if rp:
        try:
            summary["rules_deleted"] = cr.delete_for_source(rp, tenant_id=tid)
        except Exception:  # noqa: BLE001
            logger.exception("forget: rules cleanup failed for %s", rp)

    # 4. mark forgotten.
    try:
        cm.mark_forgotten(row["id"])
    except Exception:  # noqa: BLE001
        logger.exception("forget: could not mark manifest row forgotten")

    return summary


def _locate_row(
    *,
    sha256: Optional[str],
    raw_path: Optional[str],
    file_id: Optional[str],
    tenant_id: str,
) -> Optional[Dict[str, Any]]:
    """Find an ingested_files row by SHA, raw_path, or id."""
    if sha256:
        return cm.existing_for_sha(sha256, tenant_id=tenant_id)
    if file_id:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "SELECT id, raw_path, status FROM ingested_files "
                    "WHERE id = ? AND tenant_id = ?",
                    (file_id, tenant_id),
                )
                row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "raw_path": row[1], "status": row[2]}
    if raw_path:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "SELECT id, raw_path, status FROM ingested_files "
                    "WHERE raw_path = ? AND tenant_id = ?",
                    (raw_path, tenant_id),
                )
                row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "raw_path": row[1], "status": row[2]}
    return None

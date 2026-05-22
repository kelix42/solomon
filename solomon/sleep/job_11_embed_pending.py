"""Job 11 — Embed pending captured_items.

Captured items land in the DB without a vector — embedding is deferred
to the nightly sleep cycle so the interview phase stays snappy. This
job picks up every captured_items row that doesn't yet have a matching
``embeddings`` row (``source_table='captured_items'``,
``source_id='captured:<id>'``) and embeds them in one batch.

The batch is capped at 32 rows per cycle to keep the job short — large
corpora bleed through over multiple nights, not all at once.

Idempotency: the LEFT JOIN excludes anything already embedded, so a
second run on the same DB inserts zero rows. ``store_section_embedding``
uses a delete-then-insert upsert keyed on (tenant_id, source_table,
source_id), so even if the LEFT JOIN were racy the worst case is one
re-embed, not a duplicate row.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("solomon.sleep.job_11")


_BATCH_CAP = 32


def _select_pending(cur: Any, tenant_id: str, limit: int) -> List[Any]:
    """Return up to `limit` captured_items rows without a matching embeddings row."""
    from ..storage.pool import execute

    execute(
        cur,
        "SELECT c.id, c.statement, c.verbatim_phrase, c.example, c.domain "
        "FROM captured_items c "
        "LEFT JOIN embeddings e "
        "  ON e.tenant_id = c.tenant_id "
        "  AND e.source_table = 'captured_items' "
        "  AND e.source_id = 'captured:' || c.id "
        "WHERE c.tenant_id = ? AND e.embedding_id IS NULL "
        "ORDER BY c.created_at ASC "
        "LIMIT ?",
        (tenant_id, limit),
    )
    return cur.fetchall()


def _row_text(row: Any) -> str:
    """Best-available textual content for embedding."""
    # captured_items columns we SELECT'd: id, statement, verbatim_phrase, example, domain
    def _g(col_idx: int, name: str) -> Optional[str]:
        if hasattr(row, "keys"):
            return row[name]
        return row[col_idx]

    parts: List[str] = []
    for idx, key in ((1, "statement"), (2, "verbatim_phrase"), (3, "example")):
        v = _g(idx, key)
        if v:
            parts.append(str(v))
    return "\n".join(parts).strip()


def _mark_embedded(cur: Any, captured_id: str) -> None:
    from ..storage.pool import execute
    execute(
        cur,
        "UPDATE captured_items SET embedded_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), captured_id),
    )


def run(*, tenant_id: str, batch_cap: int = _BATCH_CAP, **kwargs: Any) -> Dict[str, Any]:
    """Embed unembedded captured_items in one capped batch."""
    from ..corpus import embed as ce
    from ..storage.pool import cursor, get_conn

    embedded = 0
    skipped_empty = 0

    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                rows = _select_pending(cur, tenant_id, batch_cap)
    except Exception as e:  # noqa: BLE001
        logger.warning("Job 11 embed pending failed at SELECT: %s", e)
        return {"items_processed": 0, "embedded": 0, "tokens": 0}

    if not rows:
        logger.info("Job 11 embed pending: nothing to embed")
        return {
            "items_processed": 0,
            "embedded": 0,
            "skipped_empty": 0,
            "tokens": 0,
        }

    # store_section_embedding takes a single text + writes a single row.
    # We loop instead of using store_chunk_embeddings because each row is
    # one captured_item, not a list of chunks of the same document.
    for row in rows:
        captured_id = row[0] if not hasattr(row, "keys") else row["id"]
        domain = row[4] if not hasattr(row, "keys") else row["domain"]
        text = _row_text(row)
        if not text:
            skipped_empty += 1
            continue
        try:
            eid = ce.store_section_embedding(
                source_id=f"captured:{captured_id}",
                text=text,
                source_table=ce.SOURCE_TABLE_CAPTURED,
                metadata={"captured_id": captured_id, "domain": domain},
                tenant_id=tenant_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Job 11: embedding %s failed: %s", captured_id, e)
            continue
        if eid is None:
            continue

        # Mark embedded_at on the captured row so the index
        # (idx_captured_pending_embed) shrinks and the LEFT JOIN above
        # converges faster.
        try:
            with get_conn() as conn:
                with cursor(conn) as cur:
                    _mark_embedded(cur, captured_id)
                conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("Job 11: marking %s embedded failed: %s", captured_id, e)

        embedded += 1

    logger.info(
        "Job 11 embed pending: %d/%d embedded (%d skipped — empty text)",
        embedded, len(rows), skipped_empty,
    )
    return {
        "items_processed": len(rows),
        "embedded": embedded,
        "skipped_empty": skipped_empty,
        "tokens": 0,
    }

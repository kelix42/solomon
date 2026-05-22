"""Job 2 — Rule archival (Part 16, Part 24).

For every active heuristic with last_used_at > 90 days ago, set status
to 'archived'. Archived heuristics stay searchable via semantic retrieval
but are filtered out of the active hot set. Does NOT touch confidence
(see Part 24, "Disuse does not change confidence").
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

logger = logging.getLogger("solomon.sleep.job_2")


def run(*, tenant_id: str, **kwargs: Any) -> Dict[str, Any]:
    from ..storage.pool import get_pool

    cutoff_days = 90
    cutoff = datetime.now(timezone.utc) - timedelta(days=cutoff_days)
    archived = 0
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE heuristics SET status='archived' "
                    "WHERE tenant_id=%s AND status='active' "
                    "AND (last_used_at IS NULL OR last_used_at < %s);",
                    (tenant_id, cutoff),
                )
                archived = cur.rowcount or 0
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("Job 2 archival failed: %s", e)

    logger.info("Job 2 archived %d heuristics for tenant %s", archived, tenant_id)
    return {"items_processed": archived, "tokens": 0}

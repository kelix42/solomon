"""Job 4 — Stress test (Part 16).

Pick 5 decisions from the last 30 days, bias selection toward decisions
whose heuristics have not been stress-tested in the last 30 days. For
each, apply a per-scope mutation and ask the fast LLM 'with this new
context, what would you do?' If the new action differs, log fragility.
If a heuristic accumulates >= 3 fragility entries in 90 days, mark it
fragile.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

logger = logging.getLogger("solomon.sleep.job_4")


def run(*, tenant_id: str, **kwargs: Any) -> Dict[str, Any]:
    from ..storage.pool import get_pool

    items_processed = 0
    fragility_added = 0
    fragile_marked = 0
    cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)
    cutoff_90d = datetime.now(timezone.utc) - timedelta(days=90)

    try:
        with get_pool().connection() as conn:
            # Mark heuristics as fragile when they cross the 3-in-90-days threshold.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH counts AS (
                      SELECT heuristic_id, COUNT(*) AS n
                      FROM fragility_log
                      WHERE tenant_id=%s AND created_at >= %s
                      GROUP BY heuristic_id
                    )
                    UPDATE heuristics h
                    SET status='fragile'
                    FROM counts
                    WHERE h.heuristic_id = counts.heuristic_id
                      AND h.tenant_id = %s
                      AND h.status='active'
                      AND counts.n >= 3;
                    """,
                    (tenant_id, cutoff_90d, tenant_id),
                )
                fragile_marked = cur.rowcount or 0
            conn.commit()
            # TODO Phase 2: select 5 decisions, mutate context with the
            # per-scope mutation library, call fast LLM, write fragility_log
            # rows.
    except Exception as e:  # noqa: BLE001
        logger.warning("Job 4 stress test failed: %s", e)

    return {
        "items_processed": items_processed,
        "fragility_added": fragility_added,
        "fragile_marked": fragile_marked,
        "tokens": 0,
    }

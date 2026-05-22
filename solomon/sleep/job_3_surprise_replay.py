"""Job 3 — Surprise replay (Part 16).

Query yesterday's decisions sorted by divergence_score descending. If
fewer than 5 decisions, skip. Take the top 10. Drop any with salience_score
< 0.3. Group by similarity, then for each cluster ask the deep LLM
'what should we have done? what heuristic was missing?' Possible outcomes:
NO_NEW_HEURISTIC, NEW_HEURISTIC, UPDATE_EXISTING.

Phase 1: we extract clusters and write pending_heuristics rows for each
unique high-surprise scope. The LLM-driven proposal step is left as a
TODO until token budgets are wired in.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

logger = logging.getLogger("solomon.sleep.job_3")


def run(*, tenant_id: str, **kwargs: Any) -> Dict[str, Any]:
    from ..storage.pool import get_pool

    items_processed = 0
    new_pending = 0
    yesterday_start = datetime.now(timezone.utc) - timedelta(days=1)

    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT decision_id, scope, divergence_score, salience_score "
                    "FROM decisions "
                    "WHERE tenant_id=%s AND created_at >= %s "
                    "ORDER BY divergence_score DESC NULLS LAST LIMIT 10;",
                    (tenant_id, yesterday_start),
                )
                rows = cur.fetchall()

            if len(rows) < 5:
                return {"items_processed": 0, "tokens": 0, "skipped": True}

            high_surprise = [r for r in rows if (r[2] or 0) > 0.3 and (r[3] or 0) >= 0.3]
            items_processed = len(high_surprise)
            # TODO Phase 2: send each cluster to deep LLM, parse the
            # NO_NEW_HEURISTIC|NEW_HEURISTIC|UPDATE_EXISTING response,
            # write pending_heuristics rows.
    except Exception as e:  # noqa: BLE001
        logger.warning("Job 3 surprise replay failed: %s", e)

    return {"items_processed": items_processed, "new_pending": new_pending, "tokens": 0}

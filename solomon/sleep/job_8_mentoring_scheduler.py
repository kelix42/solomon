"""Job 8 — Mentoring scheduler (Part 16).

Runs last because it consumes signals from Jobs 1, 3, 4, 5, 7. Gathers
the urgent-signal counts and proposes moving the next mentoring session
within 7 days if any threshold is crossed:

  - new_pending_heuristics_7d > 5
  - newly_fragile_heuristics_7d > 3
  - new_conflicts_7d > 2
  - regret_pattern_flags_60d > 5
  - ready_for_promotion_scopes > 0
  - newly_demoted_scopes > 0

The job never auto-schedules; it only proposes. The owner picks the time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

logger = logging.getLogger("solomon.sleep.job_8")


def run(*, tenant_id: str, **kwargs: Any) -> Dict[str, Any]:
    from ..storage.pool import get_pool

    now = datetime.now(timezone.utc)
    cutoff_7d = now - timedelta(days=7)
    cutoff_60d = now - timedelta(days=60)
    propose = False
    counts = {
        "new_pending_heuristics_7d": 0,
        "newly_fragile_heuristics_7d": 0,
        "new_conflicts_7d": 0,
        "regret_pattern_flags_60d": 0,
        "ready_for_promotion_scopes": 0,
        "newly_demoted_scopes": 0,
    }

    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM pending_heuristics "
                    "WHERE tenant_id=%s AND created_at >= %s;",
                    (tenant_id, cutoff_7d),
                )
                counts["new_pending_heuristics_7d"] = int((cur.fetchone() or (0,))[0] or 0)

                cur.execute(
                    "SELECT COUNT(*) FROM heuristics "
                    "WHERE tenant_id=%s AND status='fragile' AND last_updated_at >= %s;",
                    (tenant_id, cutoff_7d),
                )
                counts["newly_fragile_heuristics_7d"] = int((cur.fetchone() or (0,))[0] or 0)

                cur.execute(
                    "SELECT COUNT(*) FROM regret_signals "
                    "WHERE tenant_id=%s AND created_at >= %s;",
                    (tenant_id, cutoff_60d),
                )
                counts["regret_pattern_flags_60d"] = int((cur.fetchone() or (0,))[0] or 0)

                cur.execute(
                    "SELECT COUNT(*) FROM autonomy_state "
                    "WHERE tenant_id=%s AND last_demoted_at >= %s;",
                    (tenant_id, cutoff_7d),
                )
                counts["newly_demoted_scopes"] = int((cur.fetchone() or (0,))[0] or 0)

        if (
            counts["new_pending_heuristics_7d"] > 5
            or counts["newly_fragile_heuristics_7d"] > 3
            or counts["new_conflicts_7d"] > 2
            or counts["regret_pattern_flags_60d"] > 5
            or counts["ready_for_promotion_scopes"] > 0
            or counts["newly_demoted_scopes"] > 0
        ):
            propose = True
    except Exception as e:  # noqa: BLE001
        logger.warning("Job 8 mentoring scheduler failed: %s", e)

    if propose:
        logger.info(
            "Mentoring session proposed for tenant %s: %s",
            tenant_id, counts,
        )

    return {
        "items_processed": sum(counts.values()),
        "propose_session": propose,
        "signals": counts,
        "tokens": 0,
    }

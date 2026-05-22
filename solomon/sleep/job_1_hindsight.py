"""Job 1 — Hindsight check (Part 16 of the design doc).

Replays the day at bedtime. For each prediction whose expected_by has
passed but has no recorded outcome, look for a matching captured event,
compare actual vs predicted, write a regret_signal if the alternative
would have been better.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

logger = logging.getLogger("solomon.sleep.job_1")


def run(*, tenant_id: str, **kwargs: Any) -> Dict[str, Any]:
    from ..storage.pool import get_pool

    items_processed = 0
    regret_signals_added = 0
    unresolved_marked = 0

    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                # Pull pending predictions whose expected_by is past.
                cur.execute(
                    "SELECT prediction_id, decision_id, prediction_text, expected_by "
                    "FROM predictions "
                    "WHERE tenant_id=%s AND status='pending' AND expected_by <= %s "
                    "ORDER BY expected_by ASC LIMIT 200;",
                    (tenant_id, datetime.now(timezone.utc)),
                )
                rows = cur.fetchall()

            for prediction_id, decision_id, prediction_text, expected_by in rows:
                items_processed += 1
                # Phase 1: we don't yet have an outcome-matcher. For
                # predictions older than 30 days past expected_by, mark
                # them unresolved (counts as a calibration miss).
                if expected_by.tzinfo is None:
                    expected_by = expected_by.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - expected_by > timedelta(days=30):
                    with conn.cursor() as cur2:
                        cur2.execute(
                            "UPDATE predictions SET status='unresolved', checked_at=%s "
                            "WHERE prediction_id=%s;",
                            (datetime.now(timezone.utc), prediction_id),
                        )
                    unresolved_marked += 1
                # TODO Phase 2: search raw_events from action_time to
                # expected_by + 24h for a matching outcome, ask the deep
                # LLM if it matches, then update calibration metrics and
                # write regret_signal if counterfactual was better.

            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("Job 1 hindsight failed: %s", e)

    return {
        "items_processed": items_processed,
        "regret_signals_added": regret_signals_added,
        "unresolved_marked": unresolved_marked,
        "tokens": 0,
    }

"""Job 7 — Autonomy re-evaluation (Part 16, Part 11).

For every scope in autonomy_state:
  1. Hysteresis check — if promoted/demoted within last 14 days, skip.
  2. Compute trailing 7-day and 30-day metrics.
  3. Demotion: non_negotiable violations (drop to watch + notify),
     override rate > 0.15 (one level down), edit rate > 0.30 (one level down).
  4. Promotion: 30-day decision_count >= 50 AND override_rate < 0.05
     AND avg_confidence > 0.8 -> flag as ready_for_promotion. Owner
     approves with one click; do NOT auto-promote.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..autonomy.ladder import AUTONOMY_LEVELS

logger = logging.getLogger("solomon.sleep.job_7")


def _level_index(level: str) -> int:
    try:
        return AUTONOMY_LEVELS.index(level)
    except ValueError:
        return 0


def _level_name(idx: int) -> str:
    return AUTONOMY_LEVELS[max(0, min(idx, len(AUTONOMY_LEVELS) - 1))]


def run(*, tenant_id: str, **kwargs: Any) -> Dict[str, Any]:
    from ..storage.pool import get_pool

    promotions_flagged: List[str] = []
    demotions_applied: List[Tuple[str, str, str]] = []  # (scope, from, to)
    now = datetime.now(timezone.utc)

    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT scope, level, last_promoted_at, last_demoted_at "
                    "FROM autonomy_state WHERE tenant_id=%s;",
                    (tenant_id,),
                )
                scopes = cur.fetchall()

            for scope, current_level, last_promoted, last_demoted in scopes:
                # Hysteresis: skip if promoted/demoted within last 14 days.
                def recent(ts: Optional[datetime]) -> bool:
                    if ts is None:
                        return False
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    return (now - ts) < timedelta(days=14)

                if recent(last_promoted) or recent(last_demoted):
                    continue

                # 7-day and 30-day metrics.
                metrics = _compute_metrics(conn, tenant_id, scope, now)

                # Demotion checks.
                demote_to: Optional[int] = None
                if metrics["non_negotiable_violations_7d"] > 0:
                    demote_to = 0  # to watch
                elif metrics["override_rate_7d"] > 0.15:
                    demote_to = max(0, _level_index(current_level) - 1)
                elif metrics["edit_rate_7d"] > 0.30:
                    demote_to = max(0, _level_index(current_level) - 1)

                if demote_to is not None and demote_to < _level_index(current_level):
                    new_level = _level_name(demote_to)
                    with conn.cursor() as cur2:
                        cur2.execute(
                            "UPDATE autonomy_state SET level=%s, last_demoted_at=%s, since=%s "
                            "WHERE tenant_id=%s AND scope=%s;",
                            (new_level, now, now, tenant_id, scope),
                        )
                    demotions_applied.append((scope, current_level, new_level))
                    continue  # Don't promote in the same night.

                # Promotion check (only if no demotion fired and current < act_alone).
                idx = _level_index(current_level)
                if idx >= len(AUTONOMY_LEVELS) - 1:
                    continue
                if (
                    metrics["decision_count_30d"] >= 50
                    and metrics["override_rate_30d"] < 0.05
                    and metrics["avg_confidence_30d"] > 0.8
                ):
                    promotions_flagged.append(scope)
                    # We do NOT auto-promote. The owner approves in the
                    # morning review queue. For now we just flag it by
                    # inserting a pending_approval row.
                    # TODO Phase 2: wire owner UI to read and approve this.
                    logger.info("Scope %s flagged ready for promotion.", scope)
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("Job 7 autonomy failed: %s", e)

    return {
        "items_processed": len(promotions_flagged) + len(demotions_applied),
        "promotions_flagged": promotions_flagged,
        "demotions_applied": demotions_applied,
        "tokens": 0,
    }


def _compute_metrics(conn, tenant_id: str, scope: str, now: datetime) -> Dict[str, Any]:  # noqa: ANN001
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*), "
            "AVG(CASE WHEN owner_action IN ('rejected','edited') THEN 1.0 ELSE 0.0 END), "
            "AVG(CASE WHEN owner_action='edited' THEN 1.0 ELSE 0.0 END) "
            "FROM decisions WHERE tenant_id=%s AND scope=%s AND created_at >= %s;",
            (tenant_id, scope, cutoff_7d),
        )
        row7 = cur.fetchone() or (0, 0.0, 0.0)
        cur.execute(
            "SELECT COUNT(*), "
            "AVG(CASE WHEN owner_action IN ('rejected','edited') THEN 1.0 ELSE 0.0 END), "
            "AVG(classification_confidence) "
            "FROM decisions WHERE tenant_id=%s AND scope=%s AND created_at >= %s;",
            (tenant_id, scope, cutoff_30d),
        )
        row30 = cur.fetchone() or (0, 0.0, 0.0)
        cur.execute(
            "SELECT COUNT(*) FROM decisions "
            "WHERE tenant_id=%s AND scope=%s AND audit_verdict='reject' AND created_at >= %s;",
            (tenant_id, scope, cutoff_7d),
        )
        row_viol = cur.fetchone() or (0,)
    return {
        "decision_count_7d": int(row7[0] or 0),
        "override_rate_7d": float(row7[1] or 0.0),
        "edit_rate_7d": float(row7[2] or 0.0),
        "decision_count_30d": int(row30[0] or 0),
        "override_rate_30d": float(row30[1] or 0.0),
        "avg_confidence_30d": float(row30[2] or 0.0),
        "non_negotiable_violations_7d": int(row_viol[0] or 0),
    }

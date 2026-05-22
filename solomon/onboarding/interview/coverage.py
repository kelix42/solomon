"""Coverage tracker — what's been probed, what's still thin.

Pure SQL. Three public functions:

  next_sub_topic(session_id, domain) → Optional[str]
      Returns the most under-probed sub_topic with gap_score > 0.4,
      or None if everything is saturated.

  refresh(session_id, domain, captured_count_delta) → None
      Hook called by the session runner after each owner turn. Increments
      turns_since_last_capture when no items landed; bumps items_captured
      and resets that counter when they did.

  is_session_complete(session_id, domain) → bool
      Drive's dual saturation rule:
        (a) every required sub_topic has items_captured >= 1, OR
        (b) turns_since_last_capture > 5 (diminishing returns).

Citation: docs/REPORT-INTERVIEW.md §1.1.4.
Drive source: skills/interview/solomon-coverage-tracker/SKILL.md.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from ...storage.pool import cursor, execute, get_conn

logger = logging.getLogger("solomon.onboarding.coverage")


def gap_score_for(probes_asked: int, items_captured: int) -> float:
    """Compute the gap score: 1.0 unprobed, decays toward 0 as items land.

    Same formula the Drive uses: each capture decrements by 1/(probes+1),
    floor 0.0. This function exists for unit tests and for the rare caller
    that wants the formula outside of a DB update.
    """
    if probes_asked < 0 or items_captured < 0:
        return 1.0
    score = 1.0
    for k in range(items_captured):
        # gap_score -= 1/(probes_asked_at_that_time + 1)
        # We don't have per-event probe counts, so use the current value
        # which is the steady-state approximation the engine relies on.
        score -= 1.0 / (probes_asked + 1)
    return max(0.0, score)


def next_sub_topic(session_id: str, domain: str) -> Optional[str]:
    """Return the sub_topic with the highest gap_score above 0.4, ties
    broken by lowest probes_asked. Returns None when nothing qualifies.
    """
    sql = (
        "SELECT sub_topic FROM coverage "
        "WHERE session_id=? AND domain=? AND gap_score > 0.4 "
        "ORDER BY gap_score DESC, probes_asked ASC LIMIT 1"
    )
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(cur, sql, (session_id, domain))
                row = cur.fetchone()
        if not row:
            return None
        return row[0] if not hasattr(row, "keys") else row["sub_topic"]
    except Exception as e:  # noqa: BLE001
        logger.warning("next_sub_topic query failed: %s", e)
        return None


def refresh(session_id: str, domain: str, captured_count_delta: int = 0) -> None:
    """Per-turn coverage update. Called by the session runner after each
    owner turn. If items landed, turns_since_last_capture is already 0'd
    by extraction.bump_coverage; if nothing landed, we bump it here.
    """
    if captured_count_delta > 0:
        return  # already handled inside extraction._bump_coverage_capture
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "UPDATE coverage SET "
                    "  turns_since_last_capture = turns_since_last_capture + 1, "
                    "  last_updated = datetime('now') "
                    "WHERE session_id=? AND domain=?",
                    (session_id, domain),
                )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("coverage refresh failed: %s", e)


def is_session_complete(session_id: str, domain: str) -> bool:
    """Apply the dual saturation rule.

    Returns True if:
      (a) Every sub_topic for the session has items_captured >= 1 AND
          gap_score < 0.4, OR
      (b) Max turns_since_last_capture across the session > 5.

    With NO coverage rows yet, returns False (we haven't even started).
    """
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "SELECT COUNT(*) AS total, "
                    "       SUM(CASE WHEN items_captured >= 1 AND gap_score < 0.4 THEN 1 ELSE 0 END) AS done, "
                    "       MAX(turns_since_last_capture) AS max_dry "
                    "FROM coverage WHERE session_id=? AND domain=?",
                    (session_id, domain),
                )
                row = cur.fetchone()
        if not row:
            return False
        total = (row[0] if not hasattr(row, "keys") else row["total"]) or 0
        done = (row[1] if not hasattr(row, "keys") else row["done"]) or 0
        max_dry = (row[2] if not hasattr(row, "keys") else row["max_dry"]) or 0
        if total == 0:
            return False
        if int(done) == int(total):
            return True
        if int(max_dry) > 5:
            return True
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("is_session_complete query failed: %s", e)
        return False


def required_field_gaps(session_id: str, required_field_ids: List[str]) -> List[str]:
    """Return the subset of required_field_ids that do NOT yet have a
    captured_items row tagged `field:<id>` in keywords. Pure SQL helper
    for Stage C of the session runner.
    """
    if not required_field_ids:
        return []
    missing: List[str] = []
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                for fid in required_field_ids:
                    tag = f"field:{fid}"
                    # JSON1-style scan: keywords is a JSON list of strings.
                    execute(
                        cur,
                        "SELECT 1 FROM captured_items "
                        "WHERE session_id=? AND keywords LIKE ? LIMIT 1",
                        (session_id, f'%"{tag}"%'),
                    )
                    row = cur.fetchone()
                    if not row:
                        missing.append(fid)
        return missing
    except Exception as e:  # noqa: BLE001
        logger.warning("required_field_gaps lookup failed: %s", e)
        return list(required_field_ids)

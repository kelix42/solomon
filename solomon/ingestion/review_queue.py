"""Part 26 Stage 7 — owner review queue.

After ingestion classifies, extracts, and mines its way through a
tenant's historical document pile, two streams of artifacts need
human eyes before they earn full citizenship in Solomon's memory:

  - **Historical decisions** (rows in `decisions` with `historical=true`
    and no `owner_action` yet): "did I really decide this? was the
    proposed_action what I actually did?"
  - **Pending heuristics** (rows in `pending_heuristics` with
    `status='pending'`): pattern proposals the miner extracted but
    that haven't been promoted to the real `heuristics` table.

This module exposes the read side (`pending_review_items`) and the
three owner actions on heuristics (approve / reject / defer). Decision
review actions live elsewhere because they share UI with the regular
mentoring loop. All DB errors are caught and logged; callers see an
empty result or `None` rather than an exception.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..storage.pool import get_pool

logger = logging.getLogger("solomon.ingestion.review")

_REVIEW_LIMIT = 50


def _row_to_dict(cur: Any, row: tuple) -> Dict[str, Any]:
    """Turn a psycopg row tuple into a dict using cursor.description."""
    if row is None:
        return {}
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def pending_review_items(tenant_id: str) -> Dict[str, List[Dict]]:
    """Return the owner's review backlog for ingestion artifacts.

    Returns a dict with two keys:
      - 'decisions': up to 50 historical decisions awaiting owner_action,
        ordered by salience_score DESC (most consequential first).
      - 'heuristics': up to 50 pending heuristics, ordered by
        support_count DESC (best-evidenced first).

    On any DB error, the corresponding list is empty and the error is
    logged; we never raise here because the UI calling this is a
    review screen that should degrade gracefully.
    """
    out: Dict[str, List[Dict]] = {"decisions": [], "heuristics": []}

    try:
        pool = get_pool()
    except Exception:
        logger.exception("pending_review_items: storage pool unavailable")
        return out

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT decision_id, tenant_id, event_id, scope, domain,
                           decision_type, salience_score, proposed_action,
                           final_action, audit_verdict, audit_reasoning,
                           historical, created_at
                      FROM decisions
                     WHERE tenant_id = %s
                       AND historical = TRUE
                       AND owner_action IS NULL
                     ORDER BY salience_score DESC NULLS LAST,
                              created_at DESC
                     LIMIT %s
                    """,
                    (tenant_id, _REVIEW_LIMIT),
                )
                for row in cur.fetchall():
                    out["decisions"].append(_row_to_dict(cur, row))
    except Exception:
        logger.exception(
            "pending_review_items: failed to load decisions for tenant=%s",
            tenant_id,
        )

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT pending_id, tenant_id, scope, proposed_condition,
                           proposed_action, source, support_count,
                           evidence_list, status, created_at
                      FROM pending_heuristics
                     WHERE tenant_id = %s
                       AND status = 'pending'
                     ORDER BY support_count DESC, created_at DESC
                     LIMIT %s
                    """,
                    (tenant_id, _REVIEW_LIMIT),
                )
                for row in cur.fetchall():
                    out["heuristics"].append(_row_to_dict(cur, row))
    except Exception:
        logger.exception(
            "pending_review_items: failed to load pending_heuristics "
            "for tenant=%s",
            tenant_id,
        )

    logger.info(
        "pending_review_items: tenant=%s decisions=%d heuristics=%d",
        tenant_id,
        len(out["decisions"]),
        len(out["heuristics"]),
    )
    return out


def approve_heuristic(tenant_id: str, pending_id: int) -> Optional[int]:
    """Promote a pending heuristic to the real heuristics table.

    Reads the `pending_heuristics` row, INSERTs a new row into
    `heuristics` with source='ingestion' and initial confidence 0.5
    (so it has to earn higher confidence through actual use), then
    marks the pending row as 'approved'. Returns the new
    heuristic_id, or None on any failure.

    Idempotency: if the pending row is missing or already non-pending,
    we don't double-insert. The check is by status, not by content,
    so two simultaneous approves can still race — that's acceptable
    here because the review UI is single-owner.
    """
    try:
        pool = get_pool()
    except Exception:
        logger.exception("approve_heuristic: storage pool unavailable")
        return None

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT pending_id, tenant_id, scope, proposed_condition,
                           proposed_action, source, support_count,
                           evidence_list, status
                      FROM pending_heuristics
                     WHERE pending_id = %s
                       AND tenant_id = %s
                    """,
                    (pending_id, tenant_id),
                )
                row = cur.fetchone()
                if row is None:
                    logger.warning(
                        "approve_heuristic: pending_id=%s tenant=%s not found",
                        pending_id,
                        tenant_id,
                    )
                    return None
                pending = _row_to_dict(cur, row)
                if pending.get("status") != "pending":
                    logger.warning(
                        "approve_heuristic: pending_id=%s already in "
                        "status=%s; skipping",
                        pending_id,
                        pending.get("status"),
                    )
                    return None

                cur.execute(
                    """
                    INSERT INTO heuristics (
                        tenant_id, scope, condition, action,
                        confidence, source, provenance, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING heuristic_id
                    """,
                    (
                        tenant_id,
                        pending["scope"],
                        pending["proposed_condition"],
                        pending["proposed_action"],
                        0.5,
                        "ingestion",
                        # JSON column — psycopg dumps dicts via the
                        # default adapter when registered, but we hand
                        # it a string to be safe across versions.
                        '{"pending_id": %d}' % pending_id,
                        "active",
                    ),
                )
                new_id_row = cur.fetchone()
                if new_id_row is None:
                    logger.error(
                        "approve_heuristic: INSERT returned no id "
                        "for pending_id=%s",
                        pending_id,
                    )
                    conn.rollback()
                    return None
                new_id = int(new_id_row[0])

                cur.execute(
                    """
                    UPDATE pending_heuristics
                       SET status = 'approved'
                     WHERE pending_id = %s
                       AND tenant_id = %s
                    """,
                    (pending_id, tenant_id),
                )
            conn.commit()
        logger.info(
            "approve_heuristic: tenant=%s pending=%s -> heuristic_id=%d",
            tenant_id,
            pending_id,
            new_id,
        )
        return new_id
    except Exception:
        logger.exception(
            "approve_heuristic: failed for tenant=%s pending=%s",
            tenant_id,
            pending_id,
        )
        return None


def _set_pending_status(
    tenant_id: str, pending_id: int, status: str
) -> None:
    """Shared helper for reject/defer. Errors are logged, not raised."""
    try:
        pool = get_pool()
    except Exception:
        logger.exception(
            "_set_pending_status: storage pool unavailable "
            "(tenant=%s pending=%s status=%s)",
            tenant_id,
            pending_id,
            status,
        )
        return

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE pending_heuristics
                       SET status = %s
                     WHERE pending_id = %s
                       AND tenant_id = %s
                    """,
                    (status, pending_id, tenant_id),
                )
                affected = cur.rowcount
            conn.commit()
        if affected == 0:
            logger.warning(
                "_set_pending_status: no row updated "
                "(tenant=%s pending=%s status=%s)",
                tenant_id,
                pending_id,
                status,
            )
        else:
            logger.info(
                "_set_pending_status: tenant=%s pending=%s -> %s",
                tenant_id,
                pending_id,
                status,
            )
    except Exception:
        logger.exception(
            "_set_pending_status: failed (tenant=%s pending=%s status=%s)",
            tenant_id,
            pending_id,
            status,
        )


def reject_heuristic(tenant_id: str, pending_id: int) -> None:
    """Mark a pending heuristic as rejected. Never promoted."""
    _set_pending_status(tenant_id, pending_id, "rejected")


def defer_heuristic(tenant_id: str, pending_id: int) -> None:
    """Mark a pending heuristic as deferred (decide later)."""
    _set_pending_status(tenant_id, pending_id, "deferred")

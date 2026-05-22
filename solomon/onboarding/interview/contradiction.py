"""Contradiction check — same-session real-time conflict detection.

Pulled apart from extraction because each new captured_items row needs to
be compared against the existing same-domain rows in the same tenant. A
small-N pairwise Sonnet call decides; on conflict, both rows update their
`conflicts_with` JSON list and a `clarification_queue` row is inserted so
the engine asks about it on the next turn.

Citation: docs/REPORT-INTERVIEW.md §1.1.5.
Drive source: skills/interview/solomon-contradiction-check/SKILL.md.

This is NOT semantic search — it's bounded SQL-by-domain plus an LLM
compare. Cheap and capped.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ...reasoning.llm import get_client
from ...storage.pool import cursor, execute, get_conn, jsonify, parse_json
from . import ELIZA_SYSTEM_PROMPT

logger = logging.getLogger("solomon.onboarding.contradiction")

CONTRADICTION_SYSTEM = (
    ELIZA_SYSTEM_PROMPT
    + "\n\nNow you are NOT speaking to the owner. You are comparing a new "
    "owner-stated claim against an earlier owner-stated claim from the SAME "
    "session, looking for contradictions.\n"
    "A contradiction is a direct conflict in rule, value, or factual claim — "
    "NOT a different scope, NOT a refinement, NOT an exception. If the new "
    "statement is a legitimate exception ('except when...'), it's NOT a "
    "contradiction. Be conservative: false positives waste owner attention.\n\n"
    "Return JSON: {\"is_conflict\": bool, \"reason\": str, \"suggested_probe\": str}. "
    "suggested_probe should be a verbatim-echo follow-up the engine can ask the "
    "owner to resolve it, e.g. 'Earlier you said X; just now Y. Which wins?'"
)

# Pairs we'll compare per call. Higher than this and we batch.
_MAX_COMPARISONS = 20


def _fetch_existing(item_id: str, tenant_id: str) -> List[Dict[str, Any]]:
    """Return earlier captured_items in the same domain and tenant, excluding
    the new row itself. Capped at _MAX_COMPARISONS most recent.
    """
    sql = (
        "SELECT id, domain, statement, verbatim_phrase FROM captured_items "
        "WHERE tenant_id=? AND id != ? "
        "AND domain = (SELECT domain FROM captured_items WHERE id=?) "
        "ORDER BY created_at DESC LIMIT ?"
    )
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(cur, sql, (tenant_id, item_id, item_id, _MAX_COMPARISONS))
                rows = cur.fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            if hasattr(r, "keys"):
                out.append({k: r[k] for k in r.keys()})
            else:
                out.append({
                    "id": r[0], "domain": r[1], "statement": r[2], "verbatim_phrase": r[3],
                })
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("existing-items fetch failed: %s", e)
        return []


def _fetch_one(item_id: str) -> Optional[Dict[str, Any]]:
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "SELECT id, tenant_id, session_id, domain, statement, "
                    "verbatim_phrase, conflicts_with FROM captured_items WHERE id=?",
                    (item_id,),
                )
                r = cur.fetchone()
        if not r:
            return None
        if hasattr(r, "keys"):
            return {k: r[k] for k in r.keys()}
        return {
            "id": r[0], "tenant_id": r[1], "session_id": r[2], "domain": r[3],
            "statement": r[4], "verbatim_phrase": r[5], "conflicts_with": r[6],
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("captured_items fetch failed: %s", e)
        return None


def _append_conflict(row_id: str, other_id: str) -> None:
    """Append `other_id` to row's conflicts_with JSON list (idempotent)."""
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(cur, "SELECT conflicts_with FROM captured_items WHERE id=?", (row_id,))
                r = cur.fetchone()
                if not r:
                    return
                raw = r[0] if not hasattr(r, "keys") else r["conflicts_with"]
                lst = parse_json(raw) or []
                if not isinstance(lst, list):
                    lst = []
                if other_id not in lst:
                    lst.append(other_id)
                execute(
                    cur,
                    "UPDATE captured_items SET conflicts_with=? WHERE id=?",
                    (jsonify(lst), row_id),
                )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("conflicts_with update failed: %s", e)


def check(new_item_id: str, tenant_id: str) -> List[int]:
    """Run pairwise contradiction checks for one newly inserted item.

    Returns the list of newly inserted `clarification_queue.clarification_id`
    row IDs. Empty list on no conflict or LLM failure.
    """
    new_row = _fetch_one(new_item_id)
    if not new_row:
        return []

    existing = _fetch_existing(new_item_id, tenant_id)
    if not existing:
        return []

    client = get_client()
    inserted: List[int] = []

    for prior in existing:
        user_prompt = (
            f"Domain: {new_row.get('domain')}\n\n"
            f"Earlier claim (id={prior['id']}):\n"
            f"  statement: {prior.get('statement')}\n"
            f"  verbatim: {prior.get('verbatim_phrase')}\n\n"
            f"New claim (id={new_row['id']}):\n"
            f"  statement: {new_row.get('statement')}\n"
            f"  verbatim: {new_row.get('verbatim_phrase')}\n\n"
            "Is the new claim in direct contradiction with the earlier claim? "
            "Return JSON as specified in the system prompt."
        )
        try:
            resp = client.call(
                tier="fast",
                system=CONTRADICTION_SYSTEM,
                user=user_prompt,
                json_mode=True,
                max_tokens=256,
                temperature=0.1,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Contradiction LLM call failed: %s", e)
            continue

        parsed = client.parse_json(resp.text) or {}
        if not parsed.get("is_conflict"):
            continue
        reason = (parsed.get("reason") or "").strip()
        probe = (parsed.get("suggested_probe") or "").strip()
        if not probe:
            probe = (
                f"Earlier you said: \"{prior.get('verbatim_phrase')}\". "
                f"Just now: \"{new_row.get('verbatim_phrase')}\". Which one wins, and why?"
            )

        # Insert clarification + cross-link conflicts_with on both rows.
        try:
            with get_conn() as conn:
                with cursor(conn) as cur:
                    execute(
                        cur,
                        "INSERT INTO clarification_queue "
                        "(tenant_id, session_id, new_item_id, conflicting_item_id, "
                        " reason, status) VALUES (?, ?, ?, ?, ?, 'pending')",
                        (tenant_id, new_row["session_id"], new_row["id"], prior["id"], probe),
                    )
                    cid = getattr(cur, "lastrowid", None)
                conn.commit()
            if cid:
                inserted.append(int(cid))
            _append_conflict(new_row["id"], prior["id"])
            _append_conflict(prior["id"], new_row["id"])
            logger.info(
                "Contradiction queued (cid=%s): new=%s vs prior=%s reason=%s",
                cid, new_row["id"], prior["id"], reason[:120],
            )
        except Exception as e:  # noqa: BLE001
            logger.error("clarification insert failed: %s", e)
            continue

    return inserted

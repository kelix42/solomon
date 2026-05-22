"""Interview extraction — owner text → captured_items rows.

One LLM call per owner turn (tier='fast'). The LLM returns a JSON array of
claim objects; we insert each as a captured_items row and bump the
matching coverage sub-topic's items_captured counter (which feeds the
gap_score arithmetic in coverage.refresh).

Citation: docs/REPORT-INTERVIEW.md §1.1.2.
Drive source: skills/interview/solomon-extraction/SKILL.md.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

try:
    import ulid  # ulid-py
except ImportError:  # pragma: no cover
    ulid = None  # type: ignore

from ...reasoning.llm import get_client
from ...storage.pool import cursor, execute, get_conn, jsonify
from . import ELIZA_SYSTEM_PROMPT

logger = logging.getLogger("solomon.onboarding.extraction")

EXTRACTION_SYSTEM = (
    ELIZA_SYSTEM_PROMPT
    + "\n\nNow you are NOT speaking to the owner. You are extracting structured "
    "claims from what the owner just said, for storage in captured_items. "
    "Preserve the owner's verbatim phrasing in `verbatim_phrase`. Do not "
    "paraphrase. Do not invent claims the owner did not actually make.\n\n"
    "Return a JSON object: {\"items\": [...]} where each item has:\n"
    "  - type: one of belief | principle | non_negotiable | preference | rule | "
    "example | constraint | metric | vocabulary\n"
    "  - statement: a concise restatement, ideally close to verbatim\n"
    "  - verbatim_phrase: the exact words the owner used (substring of input)\n"
    "  - example: a concrete instance the owner gave, or null\n"
    "  - keywords: array of lowercase keyword strings\n"
    "  - confidence: stated | repeated | exemplified. Use 'exemplified' only "
    "when the owner gave a concrete instance in this same turn.\n"
    "If the owner said nothing extractable, return {\"items\": []}.\n"
)


def _new_id() -> str:
    if ulid is None:
        # Deterministic-ish fallback so tests work without ulid.
        import secrets
        return "X" + secrets.token_hex(12).upper()
    return str(ulid.new())


def _tenant_id_for_session(session_id: str) -> Optional[str]:
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(cur, "SELECT tenant_id FROM sessions WHERE session_id=?", (session_id,))
                row = cur.fetchone()
        if not row:
            return None
        return row[0] if not hasattr(row, "keys") else row["tenant_id"]
    except Exception as e:  # noqa: BLE001
        logger.warning("tenant lookup failed: %s", e)
        return None


def _bump_coverage_capture(session_id: str, domain: str, keywords: List[str]) -> None:
    """For each matched keyword, increment items_captured, decay gap_score,
    reset turns_since_last_capture. Pure SQL.
    """
    if not keywords:
        return
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                for kw in keywords:
                    execute(
                        cur,
                        "UPDATE coverage SET "
                        "  items_captured = items_captured + 1, "
                        "  turns_since_last_capture = 0, "
                        "  gap_score = MAX(0.0, gap_score - (1.0 / (probes_asked + 1))), "
                        "  last_updated = datetime('now') "
                        "WHERE session_id=? AND domain=? AND sub_topic=?",
                        (session_id, domain, kw),
                    )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("coverage capture bump failed: %s", e)


def extract(
    session_id: str,
    turn_number: int,
    owner_text: str,
    domain: str,
) -> List[str]:
    """Run extraction over the owner's redacted turn text.

    Returns a list of newly inserted captured_items.id values (ULIDs).
    Empty list on LLM failure or empty input — never raises.
    """
    if not owner_text or not owner_text.strip():
        return []

    tenant_id = _tenant_id_for_session(session_id) or "default"
    client = get_client()

    user_prompt = (
        f"Domain: {domain}\nTurn: {turn_number}\nSession: {session_id}\n\n"
        f"Owner just said:\n\"\"\"\n{owner_text}\n\"\"\"\n\n"
        "Return JSON per the schema in the system message."
    )
    try:
        resp = client.call(
            tier="fast",
            system=EXTRACTION_SYSTEM,
            user=user_prompt,
            json_mode=True,
            max_tokens=1024,
            temperature=0.1,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Extraction LLM call failed: %s", e)
        return []

    parsed = client.parse_json(resp.text) or {}
    items: List[Dict[str, Any]] = parsed.get("items") or []
    if not isinstance(items, list):
        logger.warning("Extraction returned non-list items: %r", type(items))
        return []

    new_ids: List[str] = []
    all_keywords: List[str] = []

    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    statement = (item.get("statement") or "").strip()
                    verbatim = (item.get("verbatim_phrase") or "").strip()
                    if not statement or not verbatim:
                        continue
                    item_id = _new_id()
                    kw_list = item.get("keywords") or []
                    if not isinstance(kw_list, list):
                        kw_list = []
                    confidence = (item.get("confidence") or "stated").strip().lower()
                    if confidence not in ("stated", "repeated", "exemplified"):
                        confidence = "stated"
                    type_ = (item.get("type") or "preference").strip().lower()
                    example = item.get("example")
                    execute(
                        cur,
                        "INSERT INTO captured_items "
                        "(id, tenant_id, session_id, domain, type, statement, "
                        " verbatim_phrase, example, keywords, confidence, "
                        " conflicts_with, source_session) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?)",
                        (
                            item_id, tenant_id, session_id, domain, type_,
                            statement, verbatim, example,
                            jsonify(kw_list), confidence, session_id,
                        ),
                    )
                    new_ids.append(item_id)
                    all_keywords.extend(str(k) for k in kw_list)
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to insert captured_items: %s", e)
        return []

    if new_ids:
        _bump_coverage_capture(session_id, domain, all_keywords)
        # Also bump the sessions.items_captured tally and turn_count.
        try:
            with get_conn() as conn:
                with cursor(conn) as cur:
                    execute(
                        cur,
                        "UPDATE sessions SET items_captured = items_captured + ?, "
                        "turn_count = ? WHERE session_id=?",
                        (len(new_ids), turn_number, session_id),
                    )
                conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("sessions tally update failed: %s", e)

    return new_ids

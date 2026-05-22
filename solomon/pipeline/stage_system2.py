"""Stage 7 — System 2 (deep deliberate reasoning) + 7b divergence.

Drive source: ``orchestrator/pipeline/stage_system2.py``. Report §3 lines
46-47 — "Opus, ~2000 tok. Chain-of-thought" plus the inline
``0.6·jaccard + 0.4·(1 - length_ratio)`` divergence between the System
1 and System 2 outputs.

Calls ``solomon.reasoning.llm.get_client()`` with ``tier="deep"``.
Reuses ``solomon.reasoning.divergence.divergence_score`` for the math.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..reasoning.divergence import divergence_score
from ..reasoning.llm import get_client
from ..storage.pool import jsonify, parse_json
from ._helpers import update_event
from .stage_salience import _row_to_raw_event

logger = logging.getLogger("solomon.pipeline.system2")


_SYSTEM2_PROMPT = (
    "You are Solomon's System 2: the deliberate reasoner. You see the "
    "full situation, recent decisions, and the relevant heuristics. "
    "Think carefully, then return JSON with keys:\n"
    "  - reasoning: your chain of thought (a few sentences)\n"
    "  - proposed_action: the concrete action you recommend\n"
    "  - confidence: a float in [0, 1]\n"
    "Return ONLY a JSON object."
)


def _extract_s1_text(event_row: dict) -> str:
    """Pull a plain-string System 1 answer out of the row for divergence."""
    s1 = event_row.get("system1_output")
    if s1 is None:
        return ""
    if isinstance(s1, dict):
        return str(s1.get("answer") or "")
    parsed = parse_json(s1)
    if isinstance(parsed, dict):
        return str(parsed.get("answer") or "")
    return str(s1)


def run(event_id: str, event_row: dict, *, adapter: Optional[Any] = None) -> dict:
    """Run deep reasoning, then compute divergence vs System 1.

    Writes:
      - ``system2_output`` as JSON ``{reasoning, proposed_action,
        confidence}``
      - ``divergence_score`` as a float in [0, 1]
    """
    raw_event = _row_to_raw_event(event_row)
    classification = event_row.get("classification") or {}
    if not isinstance(classification, dict):
        classification = {}
    scope = classification.get("scope")
    retrieval = event_row.get("retrieval_context") or {}
    if not isinstance(retrieval, dict):
        retrieval = {}

    client = get_client()

    reasoning = ""
    proposed_action = ""
    confidence = 0.5

    if not client.configured:
        logger.info("stage_system2: client unconfigured; writing empty answer")
    else:
        situation = (raw_event.raw_content or "").strip()
        scope_line = f"Scope: {scope}\n" if scope else ""
        retrieval_line = (
            f"Retrieval: lanes={retrieval.get('lanes_used') or []}, "
            f"heuristics={retrieval.get('heuristic_ids') or []}"
        )
        try:
            resp = client.call(
                tier="deep",
                system=_SYSTEM2_PROMPT,
                user=(
                    f"{scope_line}"
                    f"{retrieval_line}\n\n"
                    f"Situation:\n{situation}\n\n"
                    "Respond with the JSON object now."
                ),
                json_mode=True,
                max_tokens=1024,
                temperature=0.3,
            )
            text = (resp.text or "").strip()
            parsed = client.parse_json(text) if text else None
            if isinstance(parsed, dict):
                reasoning = str(parsed.get("reasoning") or "").strip()
                proposed_action = str(parsed.get("proposed_action") or "").strip()
                try:
                    confidence = float(parsed.get("confidence", 0.5))
                except (TypeError, ValueError):
                    confidence = 0.5
                confidence = max(0.0, min(1.0, confidence))
            else:
                reasoning = text
        except Exception as e:  # noqa: BLE001
            logger.warning("stage_system2 LLM call failed: %s", e)

    payload = {
        "reasoning": reasoning,
        "proposed_action": proposed_action,
        "confidence": confidence,
    }
    s2_text = reasoning + ("\n" + proposed_action if proposed_action else "")
    s1_text = _extract_s1_text(event_row)
    div = divergence_score(s1_text, s2_text)

    update_event(event_id, system2_output=jsonify(payload), divergence_score=div)
    event_row["system2_output"] = payload
    event_row["divergence_score"] = div
    return event_row

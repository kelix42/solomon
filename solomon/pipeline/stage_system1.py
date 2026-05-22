"""Stage 6 — System 1 (fast intuitive answer).

Drive source: ``orchestrator/pipeline/stage_system1.py``. Report §3 line
45 — "Sonnet, ~200 tok. Rules-only, 1–2 sentences".

Thin wrapper over ``solomon.reasoning.system_1.System1``. Reads the
event's classification + retrieved context from the events row, calls
the system-1 module which goes through
``solomon.reasoning.llm.get_client()`` with ``tier="fast"``, then
writes the JSON-encoded output into ``events.system1_output`` via the
``jsonify`` helper.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..reasoning.llm import get_client
from ..storage.pool import jsonify
from ._helpers import update_event
from .stage_salience import _row_to_raw_event

logger = logging.getLogger("solomon.pipeline.system1")


_SYSTEM1_PROMPT = (
    "You are Solomon's System 1. Apply the owner's stated rules. "
    "Return the rule-based answer in 1-2 sentences. "
    "NO reasoning. NO exploration. Just pattern-match."
)


def run(event_id: str, event_row: dict, *, adapter: Optional[Any] = None) -> dict:
    """Run the fast pattern-matcher. Writes system1_output as a JSON blob.

    Writes a dict ``{"answer": str, "confidence": float, "scope": str|None}``
    so downstream stages (divergence, audit, mirror_event_to_decision)
    can extract pieces without re-parsing.
    """
    raw_event = _row_to_raw_event(event_row)
    classification = event_row.get("classification") or {}
    if not isinstance(classification, dict):
        classification = {}
    scope = classification.get("scope")
    retrieval = event_row.get("retrieval_context") or {}
    if not isinstance(retrieval, dict):
        retrieval = {}
    heuristic_ids = list(retrieval.get("heuristic_ids") or [])

    client = get_client()
    answer = ""
    confidence = 0.5

    if not client.configured:
        logger.info("stage_system1: client unconfigured; writing empty answer")
    else:
        # Build the prompt inline so we control tier= explicitly per the
        # session-A contract. (The legacy System1 class also routes through
        # get_client(tier="fast"), but we want the stage to be self-contained.)
        situation = (raw_event.raw_content or "").strip()
        scope_line = f"Scope: {scope}\n" if scope else ""
        heur_line = (
            "Heuristics: " + ", ".join(str(h) for h in heuristic_ids[:8])
            if heuristic_ids
            else "Heuristics: (none loaded)"
        )
        try:
            resp = client.call(
                tier="fast",
                system=_SYSTEM1_PROMPT,
                user=(
                    f"{scope_line}"
                    f"{heur_line}\n\n"
                    f"Situation:\n{situation}\n\n"
                    "Answer:"
                ),
                max_tokens=256,
                temperature=0.1,
            )
            answer = (resp.text or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("stage_system1 LLM call failed: %s", e)
            answer = ""

    payload = {
        "answer": answer,
        "confidence": confidence,
        "scope": scope,
    }
    update_event(event_id, system1_output=jsonify(payload))
    event_row["system1_output"] = payload
    return event_row

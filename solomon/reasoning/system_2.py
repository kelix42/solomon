"""Part 9: System 2 — deep, deliberate pass.

Expensive Opus call. We hand it everything we have: the raw event, scope,
recent hot-context decisions, and the same heuristics System 1 saw. We ask
for explicit reasoning, a proposed action, and a self-rated confidence.

The gap between this answer and System 1's snap judgment is the surprise
signal Solomon uses to decide whether to wake the audit gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from .llm import get_client
from ..storage.pool import get_pool

logger = logging.getLogger("solomon.reasoning")


@dataclass
class S2Answer:
    answer: str
    proposed_action: str
    confidence: float


class System2:
    """Deliberate reasoner. Reads everything, produces a structured plan."""

    def __init__(self, adapter) -> None:  # noqa: ANN001
        self.adapter = adapter

    def _load_heuristics(self, heuristic_ids: List[str]) -> List[str]:
        if not heuristic_ids:
            return []
        try:
            pool = get_pool()
        except Exception as e:  # noqa: BLE001
            logger.warning("System2: could not get DB pool: %s", e)
            return []
        rows: List[str] = []
        try:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT heuristic_id, condition, action "
                        "FROM heuristics WHERE heuristic_id = ANY(%s);",
                        (list(heuristic_ids),),
                    )
                    for hid, condition, action in cur.fetchall():
                        rows.append(f"- [{hid}] IF {condition} THEN {action}")
        except Exception as e:  # noqa: BLE001
            logger.warning("System2: heuristic lookup failed: %s", e)
            return []
        return rows

    def _format_context(self, context: Any) -> str:
        """Format HotContext.items as a 'recent decisions' block."""
        if context is None:
            return "(no recent context available)"
        items = getattr(context, "items", None)
        if not items:
            return "(no recent context available)"
        lines: List[str] = []
        for it in items:
            try:
                scope = it.get("scope") or "?"
                domain = it.get("domain") or "?"
                salience = it.get("salience")
                ans = (it.get("system_2_answer") or "").strip()
                if len(ans) > 240:
                    ans = ans[:240] + "..."
                sal_str = f"{salience:.2f}" if isinstance(salience, (int, float)) else "?"
                lines.append(
                    f"- scope={scope} domain={domain} salience={sal_str}: {ans}"
                )
            except Exception:  # noqa: BLE001
                continue
        return "\n".join(lines) if lines else "(no recent context available)"

    def reason(
        self,
        raw_event,  # noqa: ANN001
        scope: Optional[str],
        context,  # noqa: ANN001
        heuristic_ids: List[str],
    ) -> S2Answer:
        """Deep reasoning pass. Never crashes."""
        client = get_client()
        if not client.configured:
            logger.info("System2: LLM unconfigured, returning empty answer.")
            return S2Answer("", "", 0.0)

        # Situation extraction.
        situation = ""
        try:
            if isinstance(raw_event, dict):
                situation = str(raw_event.get("content") or raw_event)
            else:
                situation = str(getattr(raw_event, "content", raw_event))
        except Exception:  # noqa: BLE001
            situation = repr(raw_event)

        heuristic_lines = self._load_heuristics(heuristic_ids)
        heuristics_block = (
            "\n".join(heuristic_lines)
            if heuristic_lines
            else "(no specific heuristics loaded)"
        )
        recent_decisions = self._format_context(context)
        scope_line = f"Scope: {scope}\n" if scope else ""

        system_prompt = (
            "You are System 2: the deliberate reasoner. You see the full "
            "situation, recent decisions, and the relevant heuristics. "
            "Think carefully, then output JSON with keys:\n"
            "  - reasoning: your chain of thought (a few sentences)\n"
            "  - proposed_action: the concrete action you recommend\n"
            "  - confidence: a float in [0, 1]\n"
            "Return ONLY a JSON object."
        )
        user_prompt = (
            f"{scope_line}"
            f"Situation:\n{situation}\n\n"
            f"Recent decisions:\n{recent_decisions}\n\n"
            f"Heuristics:\n{heuristics_block}\n\n"
            "Respond with the JSON object now."
        )

        try:
            resp = client.call(
                tier="deep",
                system=system_prompt,
                user=user_prompt,
                json_mode=True,
                max_tokens=1024,
                temperature=0.3,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("System2: LLM call raised: %s", e)
            return S2Answer("", "", 0.0)

        text = (resp.text or "").strip()
        if not text:
            return S2Answer("", "", 0.0)

        parsed = client.parse_json(text)
        if not parsed or not isinstance(parsed, dict):
            logger.info("System2: JSON parse failed; using raw text as answer.")
            return S2Answer(answer=text, proposed_action="", confidence=0.5)

        reasoning = str(parsed.get("reasoning") or "").strip()
        proposed_action = str(parsed.get("proposed_action") or "").strip()
        confidence_raw = parsed.get("confidence", 0.5)
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = 0.5
        # Clamp.
        if confidence < 0.0:
            confidence = 0.0
        elif confidence > 1.0:
            confidence = 1.0

        return S2Answer(
            answer=reasoning or text,
            proposed_action=proposed_action,
            confidence=confidence,
        )

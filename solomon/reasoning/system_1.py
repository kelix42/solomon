"""Part 9: System 1 — fast, pattern-matching pass.

Cheap Sonnet call. We hand it only the loaded heuristics (no full hot context,
no docs, no audit chain) and tell it not to reason: just pattern-match. The
point is to get a quick, cheap baseline answer we can compare against the
slower System 2 pass. Big divergence between the two is the surprise signal
that drives the audit gate and the nightly sleep cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from .llm import get_client
from ..storage.pool import get_pool

logger = logging.getLogger("solomon.reasoning")


@dataclass
class S1Answer:
    answer: str
    confidence: float = 0.5


class System1:
    """Fast pattern-matcher. Returns a single short answer, no reasoning."""

    def __init__(self, adapter) -> None:  # noqa: ANN001
        self.adapter = adapter

    def _load_heuristics(self, heuristic_ids: List[str]) -> List[str]:
        """Pull condition/action text for the given heuristic IDs."""
        if not heuristic_ids:
            return []
        try:
            pool = get_pool()
        except Exception as e:  # noqa: BLE001
            logger.warning("System1: could not get DB pool: %s", e)
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
            logger.warning("System1: heuristic lookup failed: %s", e)
            return []
        return rows

    def predict(
        self,
        raw_event,  # noqa: ANN001
        scope: Optional[str],
        heuristics: List[str],
    ) -> S1Answer:
        """Fast pattern-match. Never crashes."""
        client = get_client()
        if not client.configured:
            logger.info("System1: LLM unconfigured, returning empty answer.")
            return S1Answer("", 0.0)

        heuristic_lines = self._load_heuristics(heuristics)
        if heuristic_lines:
            heuristics_block = "\n".join(heuristic_lines)
            heuristics_note = (
                "Here are the relevant heuristics:\n"
                f"{heuristics_block}"
            )
        else:
            heuristics_note = (
                "No specific heuristics; apply general judgment briefly."
            )

        # Pull a compact view of the situation. raw_event may be dict-like or
        # an object with a `content` attr — be tolerant.
        situation = ""
        try:
            if isinstance(raw_event, dict):
                situation = str(raw_event.get("content") or raw_event)
            else:
                situation = str(getattr(raw_event, "content", raw_event))
        except Exception:  # noqa: BLE001
            situation = repr(raw_event)

        scope_line = f"Scope: {scope}\n" if scope else ""

        system_prompt = (
            "You are System 1: a fast pattern-matcher. "
            "Here are the relevant heuristics. Here is the situation. "
            "Don't reason. Just pattern-match. "
            "Give a single short answer (one or two sentences max)."
        )
        user_prompt = (
            f"{heuristics_note}\n\n"
            f"{scope_line}"
            f"Situation:\n{situation}\n\n"
            "Answer:"
        )

        try:
            resp = client.call(
                tier="fast",
                system=system_prompt,
                user=user_prompt,
                max_tokens=256,
                temperature=0.1,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("System1: LLM call raised: %s", e)
            return S1Answer("", 0.0)

        text = (resp.text or "").strip()
        if not text:
            return S1Answer("", 0.0)
        return S1Answer(answer=text, confidence=0.5)

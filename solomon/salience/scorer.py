"""Salience scorer — rates how much each captured event matters.

See Part 3 of the design doc. Every RawEvent that flows through the
conductor gets scored on four dimensions, each 0.0–1.0:

  - stakes:            how consequential is this for the business?
                       (revenue, hiring, legal, customer trust, etc.)
  - novelty:           is this a new situation, or another instance of
                       something the owner sees daily?
  - emotion:           how charged is the language / situation?
                       (conflict, excitement, frustration, fear)
  - owner_involvement: is the owner directly in the loop, or is this
                       background noise from systems and bystanders?

The four dimensions are averaged with configurable weights (defaults
stakes 0.40, novelty 0.30, emotion 0.15, owner_involvement 0.15) into
a single salience_score in [0.0, 1.0]. Higher salience → more downstream
attention from classification, System 2, audit gate, and the sleep cycle.

Implementation is a single fast-tier LLM call (Sonnet) in JSON mode.
If the call fails or returns junk, we fall back to a neutral default of
0.3 across the board — the conductor's hot path must never crash on a
salience scoring error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..reasoning.llm import LLMClient, get_client

logger = logging.getLogger("solomon.salience")


# Default weights for the four dimensions. Must sum to 1.0.
_DEFAULT_WEIGHTS: Dict[str, float] = {
    "stakes": 0.40,
    "novelty": 0.30,
    "emotion": 0.15,
    "owner_involvement": 0.15,
}

_DIMENSIONS = ("stakes", "novelty", "emotion", "owner_involvement")

# Neutral fallback when the LLM is unreachable or returns garbage. 0.3 is
# low enough that it won't trigger expensive downstream processing, but
# high enough that we don't silently drop everything during an outage.
_FALLBACK_SCORE: float = 0.3


@dataclass
class SalienceResult:
    """Result of scoring a single RawEvent."""

    score: float
    breakdown: Dict[str, float] = field(default_factory=dict)


class SalienceScorer:
    """Scores RawEvents on the four salience dimensions.

    One instance per conductor. Holds a reference to the Solomon adapter
    so it can read weight overrides from config, and uses the module-level
    LLM client singleton for the actual call.
    """

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter
        self._llm: LLMClient = get_client()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def score(self, raw_event: Any, scope: Optional[str] = None) -> SalienceResult:
        """Score one RawEvent. Always returns a SalienceResult — never raises.

        `scope` is the business domain hint (e.g. "law firm", "HVAC shop")
        that helps the model calibrate what "high stakes" looks like for
        this particular tenant. If omitted, the prompt stays generic.
        """
        try:
            breakdown = self._call_llm(raw_event, scope)
        except Exception as e:  # noqa: BLE001 — never crash the conductor
            logger.warning("Salience LLM call raised: %s. Falling back.", e)
            breakdown = None

        if not breakdown:
            return self._fallback()

        weights = self._load_weights()
        final = 0.0
        for dim in _DIMENSIONS:
            final += weights[dim] * breakdown[dim]
        final = max(0.0, min(1.0, final))

        logger.debug(
            "Salience for event %s: score=%.3f breakdown=%s",
            getattr(raw_event, "id", "?"),
            final,
            breakdown,
        )
        return SalienceResult(score=final, breakdown=breakdown)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _call_llm(self, raw_event: Any, scope: Optional[str]) -> Optional[Dict[str, float]]:
        """Make the fast-tier JSON call and parse the four dimensions out.

        Returns a normalized {dim: float in [0,1]} dict, or None if the
        response is missing / malformed.
        """
        system = (
            "You rate how much a captured business event matters, on four "
            "dimensions, each a float in [0.0, 1.0]:\n"
            "  - stakes: how consequential is this for the business "
            "(revenue, legal, hiring, customer trust)?\n"
            "  - novelty: is this a new situation, or a routine repeat?\n"
            "  - emotion: how emotionally charged is the language or "
            "situation (conflict, fear, excitement, frustration)?\n"
            "  - owner_involvement: is the business owner directly in the "
            "loop, or is this background noise?\n"
            "Respond with a JSON object with exactly these four keys, "
            "each a float between 0.0 and 1.0. No prose, no explanation."
        )

        scope_line = f"Business scope: {scope}\n" if scope else ""
        source = getattr(raw_event, "source", "unknown")
        participants = getattr(raw_event, "participants", []) or []
        content = getattr(raw_event, "raw_content", "") or ""

        # Cap content length so we don't blow the context window on a
        # multi-megabyte email body. The first ~4k chars are plenty for a
        # gestalt judgment.
        if len(content) > 4000:
            content = content[:4000] + "\n…[truncated]"

        user = (
            f"{scope_line}"
            f"Source: {source}\n"
            f"Participants: {', '.join(str(p) for p in participants) or '(none)'}\n"
            f"Content:\n{content}\n\n"
            "Rate this event."
        )

        response = self._llm.call(
            tier="fast",
            system=system,
            user=user,
            json_mode=True,
            max_tokens=200,
            temperature=0.1,
        )
        if not response.text:
            return None

        parsed = LLMClient.parse_json(response.text)
        if not isinstance(parsed, dict):
            logger.debug("Salience response not a dict: %r", response.text[:200])
            return None

        # Coerce and clamp each dimension. Missing keys → fallback value
        # for that dim only (don't throw away the whole response over one
        # absent field).
        out: Dict[str, float] = {}
        for dim in _DIMENSIONS:
            val = parsed.get(dim, _FALLBACK_SCORE)
            try:
                f = float(val)
            except (TypeError, ValueError):
                logger.debug("Salience: non-numeric %s=%r, using fallback", dim, val)
                f = _FALLBACK_SCORE
            out[dim] = max(0.0, min(1.0, f))
        return out

    def _load_weights(self) -> Dict[str, float]:
        """Pull per-dimension weight overrides from adapter config, with
        sane defaults. We do not renormalize — if an operator misconfigures
        the weights to not sum to 1.0, the final clamp keeps things bounded
        and the only consequence is a shifted dynamic range.
        """
        weights: Dict[str, float] = {}
        for dim, default in _DEFAULT_WEIGHTS.items():
            try:
                w = self._adapter.get_config(f"solomon.salience_weights.{dim}", default)
                weights[dim] = float(w)
            except Exception:  # noqa: BLE001
                weights[dim] = default
        return weights

    @staticmethod
    def _fallback() -> SalienceResult:
        return SalienceResult(
            score=_FALLBACK_SCORE,
            breakdown={dim: _FALLBACK_SCORE for dim in _DIMENSIONS},
        )

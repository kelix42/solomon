"""Counterfactual reasoning — Part 13 of the design doc.

For every important decision (salience >= 0.4), Solomon records what it
*didn't* do: the most plausible alternative action and the outcome it
expected from that alternative. Later, when the actual outcome of the
chosen action lands, `evaluate()` asks the deep LLM whether the
alternative would have been better. Over time this is what teaches
Solomon — and the operator — when its instincts are leading it astray.

If the deep LLM is unavailable or returns malformed JSON, we skip
silently. A missing counterfactual is never worth crashing a turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..reasoning.llm import get_client
from ..storage.decisions import get_or_create_tenant_id
from ..storage.pool import get_pool

logger = logging.getLogger("solomon.predictions.counterfactuals")


@dataclass
class Counterfactual:
    counterfactual_id: Optional[int]
    decision_id: int
    alternative_choice: str
    predicted_outcome: str
    evaluated_at: Optional[datetime] = None
    would_have_been_better: Optional[bool] = None


_GENERATE_SYSTEM = (
    "You are a counterfactual reasoner for a business co-pilot. Given an "
    "action that was actually taken, identify the single most plausible "
    "alternative the operator could have chosen, and predict the outcome "
    "that alternative would most likely have produced."
)

_GENERATE_USER_TEMPLATE = (
    "Given the actual chosen action, what is the most plausible alternative "
    "we could have done, and what outcome would that alternative have "
    "produced? JSON: "
    '{"alternative": str, "predicted_outcome": str}.\n\n'
    "Chosen action:\n{action}"
)

_EVALUATE_SYSTEM = (
    "You compare an actual outcome with a counterfactual's predicted "
    "outcome, and decide whether the alternative path would, on balance, "
    "have been better for the business."
)

_EVALUATE_USER_TEMPLATE = (
    "We chose an action and observed this actual outcome:\n"
    "ACTUAL: {actual}\n\n"
    "The alternative we considered was:\n"
    "ALTERNATIVE: {alternative}\n"
    "PREDICTED OUTCOME OF ALTERNATIVE: {predicted}\n\n"
    "Would the alternative have been better than what actually happened? "
    'Reply in JSON: {{"would_have_been_better": bool, "reasoning": str}}.'
)


class CounterfactualStore:
    """Generates, stores, and later evaluates counterfactuals."""

    def __init__(self, adapter) -> None:  # noqa: ANN001
        self.adapter = adapter

    def store_for_decision(self, decision_id: int, turn) -> Optional[int]:  # noqa: ANN001
        """Generate and persist a counterfactual for an important decision.

        Returns the new counterfactual_id, or None if salience was too low,
        the LLM is unavailable, or anything downstream failed.
        """
        salience = getattr(turn, "salience_score", None) or 0.0
        if salience < 0.4:
            return None

        action = (
            getattr(turn, "system_2_answer", None)
            or getattr(turn, "proposed_action", None)
            or getattr(turn, "final_action", None)
            or ""
        )
        if not action:
            return None

        client = get_client()
        if not client.configured:
            logger.debug("LLM not configured; skipping counterfactual generation.")
            return None

        try:
            resp = client.call(
                tier="deep",
                system=_GENERATE_SYSTEM,
                user=_GENERATE_USER_TEMPLATE.format(action=action),
                json_mode=True,
                max_tokens=512,
                temperature=0.3,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Counterfactual LLM call failed: %s", e)
            return None

        data = client.parse_json(resp.text)
        if not data:
            return None
        alternative = (data.get("alternative") or "").strip()
        predicted = (data.get("predicted_outcome") or "").strip()
        if not alternative or not predicted:
            return None

        tenant_id = get_or_create_tenant_id()
        try:
            with get_pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO counterfactuals (
                            tenant_id, decision_id, alternative_choice,
                            predicted_outcome
                        ) VALUES (%s, %s, %s, %s)
                        RETURNING counterfactual_id;
                        """,
                        (tenant_id, decision_id, alternative, predicted),
                    )
                    row = cur.fetchone()
                conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to store counterfactual for decision %s: %s", decision_id, e
            )
            return None

        if not row:
            return None
        cf_id = int(row[0])
        logger.info("Stored counterfactual %s for decision %s", cf_id, decision_id)
        return cf_id

    def evaluate(self, counterfactual_id: int, actual_outcome: str) -> None:
        """Compare actual_outcome against the stored predicted_outcome and
        record whether the alternative would have been better.
        """
        # Load the counterfactual row first.
        try:
            with get_pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT alternative_choice, predicted_outcome "
                        "FROM counterfactuals WHERE counterfactual_id = %s;",
                        (counterfactual_id,),
                    )
                    row = cur.fetchone()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to load counterfactual %s: %s", counterfactual_id, e
            )
            return

        if not row:
            logger.warning("Counterfactual %s not found", counterfactual_id)
            return
        alternative, predicted = row[0], row[1]

        client = get_client()
        if not client.configured:
            logger.debug(
                "LLM not configured; skipping counterfactual evaluation for %s",
                counterfactual_id,
            )
            return

        try:
            resp = client.call(
                tier="deep",
                system=_EVALUATE_SYSTEM,
                user=_EVALUATE_USER_TEMPLATE.format(
                    actual=actual_outcome,
                    alternative=alternative,
                    predicted=predicted,
                ),
                json_mode=True,
                max_tokens=512,
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Counterfactual eval LLM call failed: %s", e)
            return

        data = client.parse_json(resp.text)
        if not data or "would_have_been_better" not in data:
            return
        better = bool(data.get("would_have_been_better"))

        try:
            with get_pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE counterfactuals
                           SET would_have_been_better = %s,
                               evaluated_at = NOW()
                         WHERE counterfactual_id = %s;
                        """,
                        (better, counterfactual_id),
                    )
                conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to update counterfactual %s: %s", counterfactual_id, e
            )
            return

        logger.info(
            "Evaluated counterfactual %s: would_have_been_better=%s",
            counterfactual_id,
            better,
        )

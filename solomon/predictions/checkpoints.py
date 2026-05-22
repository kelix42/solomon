"""Checkpoint predictions — Part 13 of the design doc.

Every action that goes out is paired with one or more checkpoint
predictions of the form "we expect X by date Y". An hourly scheduled
job calls `pending_due()` to find predictions whose `expected_by` has
arrived, then `mark_outcome()` records what actually happened. The
delta between predicted and actual is what feeds calibration scoring
and surprise-driven learning later.

These calls only fire for turns with non-trivial salience
(>= 0.3) — trivial chatter shouldn't generate prediction rows. If the
deep LLM is unavailable or returns garbage, we skip silently rather
than crash the user's turn.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from ..reasoning.llm import get_client
from ..storage.decisions import get_or_create_tenant_id
from ..storage.pool import get_pool

logger = logging.getLogger("solomon.predictions")


@dataclass
class Prediction:
    prediction_id: Optional[int]
    decision_id: int
    prediction_text: str
    expected_by: datetime
    status: str = "pending"
    actual_outcome: Optional[str] = None
    checked_at: Optional[datetime] = None


_PREDICTION_SYSTEM = (
    "You generate checkpoint predictions for actions an AI business co-pilot "
    "is about to take. Predictions must be specific, time-bound, and "
    "verifiable from observable outcomes."
)

_PREDICTION_USER_TEMPLATE = (
    "Given the proposed action, list 1-3 specific checkpoint predictions in "
    'JSON: [{"prediction": str, "expected_by_days": int}]. '
    "Be specific and verifiable.\n\n"
    "Proposed action:\n{action}"
)


class PredictionStore:
    """CRUD for checkpoint predictions, plus LLM-driven generation."""

    def __init__(self, adapter) -> None:  # noqa: ANN001
        self.adapter = adapter

    def store_for_decision(self, decision_id: int, turn) -> List[int]:  # noqa: ANN001
        """Generate predictions for a turn's chosen action and store them.

        Returns the list of inserted prediction_ids. Returns an empty list
        when the turn is too trivial, the LLM is unavailable, or anything
        downstream fails.
        """
        salience = getattr(turn, "salience_score", None) or 0.0
        if salience < 0.3:
            return []

        action = (
            getattr(turn, "system_2_answer", None)
            or getattr(turn, "proposed_action", None)
            or getattr(turn, "final_action", None)
            or ""
        )
        if not action:
            return []

        client = get_client()
        if not client.configured:
            logger.debug("LLM not configured; skipping prediction generation.")
            return []

        try:
            resp = client.call(
                tier="deep",
                system=_PREDICTION_SYSTEM,
                user=_PREDICTION_USER_TEMPLATE.format(action=action),
                json_mode=True,
                max_tokens=512,
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Prediction LLM call failed: %s", e)
            return []

        items = self._parse_items(resp.text)
        if not items:
            return []

        tenant_id = get_or_create_tenant_id()
        now = datetime.utcnow()
        inserted: List[int] = []

        try:
            with get_pool().connection() as conn:
                with conn.cursor() as cur:
                    for item in items:
                        text = (item.get("prediction") or "").strip()
                        try:
                            days = int(item.get("expected_by_days", 7) or 7)
                        except (TypeError, ValueError):
                            days = 7
                        if not text:
                            continue
                        expected_by = now + timedelta(days=max(days, 0))
                        cur.execute(
                            """
                            INSERT INTO predictions (
                                tenant_id, decision_id, prediction_text,
                                expected_by, status
                            ) VALUES (%s, %s, %s, %s, %s)
                            RETURNING prediction_id;
                            """,
                            (tenant_id, decision_id, text, expected_by, "pending"),
                        )
                        row = cur.fetchone()
                        if row:
                            inserted.append(int(row[0]))
                conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to store predictions for decision %s: %s", decision_id, e)
            return []

        logger.info(
            "Stored %d checkpoint prediction(s) for decision %s",
            len(inserted),
            decision_id,
        )
        return inserted

    def pending_due(self) -> List[Prediction]:
        """Predictions whose expected_by has arrived and are still pending."""
        tenant_id = get_or_create_tenant_id()
        out: List[Prediction] = []
        try:
            with get_pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT prediction_id, decision_id, prediction_text,
                               expected_by, status, actual_outcome, checked_at
                          FROM predictions
                         WHERE tenant_id = %s
                           AND status = 'pending'
                           AND expected_by <= NOW()
                         ORDER BY expected_by ASC;
                        """,
                        (tenant_id,),
                    )
                    for row in cur.fetchall():
                        out.append(
                            Prediction(
                                prediction_id=int(row[0]),
                                decision_id=int(row[1]),
                                prediction_text=row[2],
                                expected_by=row[3],
                                status=row[4],
                                actual_outcome=row[5],
                                checked_at=row[6],
                            )
                        )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to query pending predictions: %s", e)
            return []
        return out

    def mark_outcome(
        self, prediction_id: int, status: str, actual_outcome: str
    ) -> None:
        """Record the verified outcome for a prediction."""
        try:
            with get_pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE predictions
                           SET status = %s,
                               actual_outcome = %s,
                               checked_at = NOW()
                         WHERE prediction_id = %s;
                        """,
                        (status, actual_outcome, prediction_id),
                    )
                conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to mark outcome for prediction %s: %s", prediction_id, e
            )

    @staticmethod
    def _parse_items(text: str) -> List[dict]:
        """Predictions can come back as either a JSON array or an object
        wrapping one (e.g. {"predictions": [...]}). Handle both.
        """
        if not text:
            return []
        t = text.strip()
        if t.startswith("```"):
            lines = t.splitlines()
            t = "\n".join(lines[1:-1]) if len(lines) >= 3 else t.strip("`")

        try:
            data = json.loads(t)
        except Exception:  # noqa: BLE001
            # Try to extract a [...] block.
            start, end = t.find("["), t.rfind("]")
            if 0 <= start < end:
                try:
                    data = json.loads(t[start:end + 1])
                except Exception:  # noqa: BLE001
                    return []
            else:
                # Maybe an object with the list inside.
                parsed = get_client().parse_json(t)
                if not parsed:
                    return []
                data = parsed

        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("predictions", "checkpoints", "items"):
                v = data.get(key)
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
        return []

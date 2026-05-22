"""Stage 2 — Salience.

Drive source: ``orchestrator/pipeline/stage_salience.py``. Report §3 line 41 —
"Haiku, ~80 tok JSON {stakes, novelty, emotion, owner_involvement, combined};
combined = max(...). If < 0.30 → ``status='skipped'``, pipeline halts."

Thin wrapper over the existing ``solomon.salience.scorer.SalienceScorer``.
We don't duplicate the LLM call — the scorer module already encapsulates
that.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from ..capture.raw_event import RawEvent
from ..salience.scorer import SalienceScorer
from ._helpers import update_event

logger = logging.getLogger("solomon.pipeline.salience")

_SALIENCE_MIN_DEFAULT = 0.30


def _salience_min() -> float:
    raw = os.getenv("SOLOMON_SALIENCE_MIN", str(_SALIENCE_MIN_DEFAULT))
    try:
        return float(raw)
    except ValueError:
        logger.warning("SOLOMON_SALIENCE_MIN=%r is not a float; using %s", raw, _SALIENCE_MIN_DEFAULT)
        return _SALIENCE_MIN_DEFAULT


@dataclass
class _StubAdapter:
    """Standalone adapter shim — SalienceScorer only needs ``get_config``.

    The Drive port keeps stage modules independent of the Hermes plugin
    surface so the worker / CLI can drive them without a live ctx.
    """
    def get_config(self, key: str, default: Any = None) -> Any:  # noqa: ANN401
        return default


def _row_to_raw_event(row: dict) -> RawEvent:
    """Reconstruct a RawEvent from the events row for downstream scorers."""
    from datetime import datetime, timezone
    received_at = row.get("received_at")
    if isinstance(received_at, str):
        try:
            ts = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            ts = datetime.now(timezone.utc)
    else:
        ts = received_at or datetime.now(timezone.utc)
    return RawEvent(
        id=str(row.get("event_id") or ""),
        source=str(row.get("source") or "unknown"),
        received_at=ts,
        participants=row.get("participants") or [],
        raw_content=str(row.get("raw_content") or ""),
        channel_metadata=row.get("channel_metadata") or {},
    )


def run(event_id: str, event_row: dict, *, adapter: Optional[Any] = None) -> dict:
    """Score salience and update the events row.

    Returns the (in-memory) event_row with ``salience_score`` populated and
    a ``status`` field if the salience floor was tripped.
    """
    raw_event = _row_to_raw_event(event_row)
    scorer = SalienceScorer(adapter or _StubAdapter())
    try:
        result = scorer.score(raw_event)
        score = float(result.score)
    except Exception as e:  # noqa: BLE001
        logger.warning("stage_salience LLM call failed: %s", e)
        score = 0.3  # Match the scorer's own fallback.

    update_event(event_id, salience_score=score)
    event_row["salience_score"] = score

    if score < _salience_min():
        logger.info("stage_salience: event %s scored %.3f < threshold %.2f → skipped",
                    event_id, score, _salience_min())
        from ._helpers import set_event_status
        set_event_status(event_id, "skipped", reason=f"salience {score:.3f} < {_salience_min():.2f}")
        event_row["status"] = "skipped"
    return event_row

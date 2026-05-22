"""Stage 3 — Classification.

Drive source: ``orchestrator/pipeline/stage_classification.py``. Report §3
line 42 — "Sonnet, ~120 tok JSON {scope, domain, decision_type}". Thin
wrapper over ``solomon.classify.classifier.Classifier``.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..classify.classifier import Classifier
from ._helpers import update_event
from .stage_salience import _StubAdapter, _row_to_raw_event

logger = logging.getLogger("solomon.pipeline.classification")


def run(event_id: str, event_row: dict, *, adapter: Optional[Any] = None) -> dict:
    """Classify scope/domain/decision_type. Writes classification JSON."""
    raw_event = _row_to_raw_event(event_row)
    classifier = Classifier(adapter or _StubAdapter())
    try:
        cls = classifier.classify(raw_event)
        classification = {
            "scope": cls.scope,
            "domain": cls.domain,
            "decision_type": cls.decision_type,
            "confidence": cls.confidence,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("stage_classification failed: %s", e)
        classification = {
            "scope": None,
            "domain": None,
            "decision_type": None,
            "confidence": 0.0,
        }

    update_event(event_id, classification=classification)
    event_row["classification"] = classification
    return event_row

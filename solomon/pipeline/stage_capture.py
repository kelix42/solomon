"""Stage 1 — Capture.

Drive source: ``orchestrator/pipeline/stage_capture.py``. Report §3 — "Stage 1
validates ``db.events`` row exists". Cheap deterministic guard; no LLM.

If the row exists, transitions status to ``in_progress``. If it's missing,
the stage returns False and the runner halts (the row should have been
inserted before ``run()`` was called).
"""
from __future__ import annotations

import logging
from typing import Optional

from ._helpers import get_event, set_event_status, update_event

logger = logging.getLogger("solomon.pipeline.capture")


def run(event_id: str) -> Optional[dict]:
    """Validate the events row. Returns the row dict, or None on failure."""
    row = get_event(event_id)
    if row is None:
        logger.error("stage_capture: event_id=%s not found", event_id)
        return None
    update_event(event_id, status="in_progress")
    # Also ensure the in-memory dict reflects the new status.
    row["status"] = "in_progress"
    return row

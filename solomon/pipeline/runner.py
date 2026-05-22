"""Pipeline runner — walks the 10 stages in order for one event.

Drive source: ``orchestrator/pipeline/runner.py``. Report §3 (the 10
stages) and §4.3 (the data-flow narrative). Halt conditions:

  * Stage 2 (salience) writes ``status='skipped'`` when the combined
    score is below the floor (default 0.30). The runner returns
    immediately.
  * Stage 4 (hard rule) writes ``status='blocked_by_hard_rule'`` on a
    JSON-logic match. The runner returns immediately.

Otherwise all 10 stages run in order. Each is wrapped in
``_helpers.stage_timer`` so per-stage elapsed_ms gets merged into the
``events.stage_timings_ms`` JSON column.

Public API:

    >>> from solomon.pipeline.runner import run
    >>> result = run("01HXJ...")   # → dict (the final events-row state)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ._helpers import get_event, stage_timer
from . import (
    stage_action,
    stage_audit,
    stage_capture,
    stage_classification,
    stage_hard_rule,
    stage_owner_state,
    stage_retrieval,
    stage_salience,
    stage_system1,
    stage_system2,
)

logger = logging.getLogger("solomon.pipeline.runner")


def run(event_id: str, *, adapter: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """Walk the 10-stage pipeline for ``event_id``.

    Returns the final events-row dict, or ``None`` if Stage 1 couldn't
    find the row. Halts early on ``status='skipped'`` (low salience) and
    ``status='blocked_by_hard_rule'``.
    """
    logger.info("pipeline start: event_id=%s", event_id)

    # ----- Stage 1: capture -------------------------------------------
    with stage_timer(event_id, "capture"):
        event_row = stage_capture.run(event_id)
    if event_row is None:
        logger.error("pipeline halt: event_id=%s not found", event_id)
        return None

    # ----- Stage 2: salience (halt-on-skipped) ------------------------
    with stage_timer(event_id, "salience"):
        event_row = stage_salience.run(event_id, event_row, adapter=adapter)
    if event_row.get("status") == "skipped":
        logger.info("pipeline skip (low salience): event_id=%s", event_id)
        return get_event(event_id) or event_row

    # ----- Stage 3: classification ------------------------------------
    with stage_timer(event_id, "classification"):
        event_row = stage_classification.run(event_id, event_row, adapter=adapter)

    # ----- Stage 4: hard-rule (halt-on-block) -------------------------
    with stage_timer(event_id, "hard_rule"):
        event_row = stage_hard_rule.run(event_id, event_row)
    if event_row.get("status") == "blocked_by_hard_rule":
        logger.info("pipeline halt (hard rule): event_id=%s", event_id)
        return get_event(event_id) or event_row

    # ----- Stage 5: retrieval -----------------------------------------
    with stage_timer(event_id, "retrieval"):
        event_row = stage_retrieval.run(event_id, event_row, adapter=adapter)

    # ----- Stage 6: System 1 ------------------------------------------
    with stage_timer(event_id, "system1"):
        event_row = stage_system1.run(event_id, event_row, adapter=adapter)

    # ----- Stage 7: System 2 (with inline divergence) -----------------
    with stage_timer(event_id, "system2"):
        event_row = stage_system2.run(event_id, event_row, adapter=adapter)

    # ----- Stage 8: audit ---------------------------------------------
    with stage_timer(event_id, "audit"):
        event_row = stage_audit.run(event_id, event_row, adapter=adapter)

    # ----- Stage 9: owner state ---------------------------------------
    with stage_timer(event_id, "owner_state"):
        event_row = stage_owner_state.run(event_id, event_row)

    # ----- Stage 10: action -------------------------------------------
    with stage_timer(event_id, "action"):
        event_row = stage_action.run(event_id, event_row)

    logger.info(
        "pipeline complete: event_id=%s action=%s verdict=%s",
        event_id, event_row.get("action_taken"), event_row.get("audit_verdict"),
    )
    # Return a fresh row read so the caller sees the merged stage_timings_ms
    # and the final status / action.
    return get_event(event_id) or event_row

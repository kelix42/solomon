"""Job 5 — Conflict detection (Part 16).

Scan yesterday's decisions for cases where two heuristics in the same
decision gave opposing advice. Auto-resolve clear cases (one confidence
> 0.8 and the other < 0.4); flag genuine ambiguities as in_conflict for
mentoring review.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

logger = logging.getLogger("solomon.sleep.job_5")


def run(*, tenant_id: str, **kwargs: Any) -> Dict[str, Any]:
    # Phase 1 scaffold: we don't yet record which two heuristics fired
    # together with opposing advice. That metadata is part of the
    # System 2 reasoning prompt output we haven't fully parsed.
    # The job runs cleanly and reports 0 conflicts.
    # TODO Phase 2: parse "opposing advice" markers from system_2_answer
    # JSON, identify pairs, run the auto-resolution + flagging logic.
    return {"items_processed": 0, "conflicts_resolved": 0, "conflicts_flagged": 0, "tokens": 0}

"""Stage 8 — Audit gate.

Drive source: ``orchestrator/pipeline/stage_audit.py``. Report §3 line 48 —
"Opus, ~300 tok. Independent gate. Returns APPROVE / DOWNGRADE / REJECT
/ REQUEST_RETHINK".

Per the Session-A prompt this stage normalizes to three verdicts:
``APPROVE``, ``REJECT``, ``REQUEST_RETHINK``. The underlying
``solomon.audit_gate.audit.AuditGate`` still emits four (``downgrade``
maps to ``APPROVE`` — the autonomy ceiling will demote the action at
Stage 10 if needed).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..audit_gate.audit import AuditGate
from ._helpers import update_event
from .stage_salience import _StubAdapter

logger = logging.getLogger("solomon.pipeline.audit")


_ALLOWED = {"APPROVE", "REJECT", "REQUEST_RETHINK"}


def _normalize_verdict(raw: str) -> str:
    """Coerce the AuditGate verdict into the 3-state Session-A schema."""
    if not raw:
        return "REQUEST_RETHINK"
    v = raw.strip().upper()
    if v in _ALLOWED:
        return v
    if v == "DOWNGRADE":
        # Downgrade means the gate approves the *content* but wants a
        # reduced autonomy level. Stage 10 handles the ceiling math, so
        # we treat downgrade as APPROVE for the verdict column.
        return "APPROVE"
    return "REQUEST_RETHINK"


def run(event_id: str, event_row: dict, *, adapter: Optional[Any] = None) -> dict:
    """Run the audit gate; write audit_verdict + audit_reasoning."""
    adapter = adapter or _StubAdapter()
    gate = AuditGate(adapter)

    system2 = event_row.get("system2_output") or {}
    if not isinstance(system2, dict):
        system2 = {}
    proposed_action = system2.get("proposed_action") or system2.get("reasoning") or ""

    classification = event_row.get("classification") or {}
    if not isinstance(classification, dict):
        classification = {}
    scope = classification.get("scope")

    divergence = event_row.get("divergence_score")
    try:
        divergence_val = float(divergence) if divergence is not None else 0.0
    except (TypeError, ValueError):
        divergence_val = 0.0

    try:
        verdict_obj = gate.run(
            proposed_action=proposed_action,
            context=None,
            surprise=divergence_val,
            scope=scope,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("stage_audit: AuditGate raised: %s", e)
        update_event(
            event_id,
            audit_verdict="REQUEST_RETHINK",
            audit_reasoning=f"audit gate raised: {type(e).__name__}",
        )
        event_row["audit_verdict"] = "REQUEST_RETHINK"
        event_row["audit_reasoning"] = f"audit gate raised: {type(e).__name__}"
        return event_row

    verdict = _normalize_verdict(getattr(verdict_obj, "verdict", "") or "")
    reasoning = str(getattr(verdict_obj, "reasoning", "") or "")

    update_event(event_id, audit_verdict=verdict, audit_reasoning=reasoning)
    event_row["audit_verdict"] = verdict
    event_row["audit_reasoning"] = reasoning
    return event_row

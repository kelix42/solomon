"""Stage 10 — Action routing.

Drive source: ``orchestrator/pipeline/stage_action.py``. Report §3 line
50 — "effective_autonomy = min(scope_level, ceil_by_state[owner_state]);
routes the four action types".

Reads:
  - ``classification.scope`` → ``scope_autonomy`` level for that scope
  - ``owner_state_ceiling`` (Stage 9 wrote this)
  - ``audit_verdict`` (Stage 8 wrote this)

Computes ``effective_autonomy = min(scope_level, owner_state_ceiling)``
and routes per ``references/autonomy-spectrum.md``:

  * verdict REJECT or REQUEST_RETHINK → ``escalate``
  * verdict APPROVE + effective L4 → ``ship``
  * verdict APPROVE + effective L2 or L3 → ``one-tap``
  * verdict APPROVE + effective L1 → ``suggest``
  * verdict APPROVE + effective L0 → ``suggest`` (Manual: never auto)

Writes ``effective_autonomy``, ``action_taken``, and ``status='complete'``.
Finally mirrors the events row into ``decisions`` via
``solomon.storage.decisions.mirror_event_to_decision`` so the long-lived
audit log gets its row.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..autonomy.ladder import scope_level as _scope_level
from ..storage.decisions import mirror_event_to_decision
from ._helpers import set_event_status, update_event

logger = logging.getLogger("solomon.pipeline.action")


def _route(verdict: str, effective: int) -> str:
    """Map (verdict, effective_autonomy) to one of four action labels."""
    v = (verdict or "").strip().upper()
    if v in {"REJECT", "REQUEST_RETHINK"}:
        return "escalate"
    # Everything below is the APPROVE branch (DOWNGRADE was normalised
    # to APPROVE in stage_audit).
    if effective >= 4:
        return "ship"
    if effective in (2, 3):
        return "one-tap"
    return "suggest"


def run(event_id: str, event_row: dict) -> dict:
    """Compute effective autonomy, route the action, finalise the row."""
    tenant_id = event_row.get("tenant_id") or "default"
    classification = event_row.get("classification") or {}
    if not isinstance(classification, dict):
        classification = {}
    scope = classification.get("scope")

    sl = _scope_level(tenant_id, scope)
    ceiling = event_row.get("owner_state_ceiling")
    try:
        ceiling_int = int(ceiling) if ceiling is not None else 4
    except (TypeError, ValueError):
        ceiling_int = 4

    effective = min(sl, ceiling_int)

    verdict = event_row.get("audit_verdict") or ""
    action = _route(verdict, effective)

    update_event(
        event_id,
        effective_autonomy=effective,
        action_taken=action,
    )
    set_event_status(event_id, "complete")
    event_row["effective_autonomy"] = effective
    event_row["action_taken"] = action
    event_row["status"] = "complete"

    # Mirror to the long-lived decisions table. Errors here log but don't
    # tank the event — the events row IS the canonical audit record;
    # decisions is the H2 mirror.
    try:
        mirror_event_to_decision(event_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("stage_action: mirror_event_to_decision failed: %s", e)

    return event_row

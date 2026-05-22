"""Test for solomon.pipeline.stage_action."""

from __future__ import annotations

import pytest

from solomon.pipeline import stage_action
from solomon.storage.pool import cursor, execute, get_conn

from tests._pipeline_helpers import read_event, reset_tenant_cache, seed_event


def _set_scope_autonomy(scope: str, level: str):
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "DELETE FROM scope_autonomy WHERE tenant_id = ? AND scope = ?",
                ("default", scope),
            )
            execute(
                cur,
                "INSERT INTO scope_autonomy (tenant_id, scope, level) VALUES (?, ?, ?)",
                ("default", scope, level),
            )
        conn.commit()


def _build_row(event_id, *, scope, ceiling, verdict):
    return {
        "event_id": event_id,
        "tenant_id": "default",
        "classification": {"scope": scope},
        "owner_state_ceiling": ceiling,
        "audit_verdict": verdict,
        "system2_output": {"proposed_action": "do it", "reasoning": "x", "confidence": 0.7},
    }


@pytest.mark.parametrize("scope_lv,ceiling,verdict,expected_action,expected_eff", [
    # Approve + scope L4 + green ceiling 4 → ship.
    ("L4", 4, "APPROVE", "ship", 4),
    # Approve + scope L3 + green → one-tap (L3 is in {2,3}).
    ("L3", 4, "APPROVE", "one-tap", 3),
    # Approve + scope L2 + green → one-tap.
    ("L2", 4, "APPROVE", "one-tap", 2),
    # Approve + scope L1 → suggest.
    ("L1", 4, "APPROVE", "suggest", 1),
    # Approve + L0 → suggest (manual: still suggest, never ship).
    ("L0", 4, "APPROVE", "suggest", 0),
    # Reject → escalate regardless of level.
    ("L4", 4, "REJECT", "escalate", 4),
    ("L2", 4, "REQUEST_RETHINK", "escalate", 2),
    # Yellow ceiling (2) caps L4 scope → effective 2 → one-tap.
    ("L4", 2, "APPROVE", "one-tap", 2),
    # Red ceiling (1) caps everything to L1 → suggest.
    ("L4", 1, "APPROVE", "suggest", 1),
])
def test_stage_action_routes(solomon_db, monkeypatch, scope_lv, ceiling, verdict, expected_action, expected_eff):
    """Each branch of the routing matrix lands the right action_taken."""
    reset_tenant_cache()
    from solomon.storage import decisions
    decisions.get_or_create_tenant_id()

    scope_name = f"scope-{scope_lv}-{ceiling}-{verdict}"
    _set_scope_autonomy(scope_name, scope_lv)
    eid = f"ev-act-{scope_lv}-{ceiling}-{verdict}"

    seed_event(
        event_id=eid,
        classification={"scope": scope_name},
        owner_state_ceiling=ceiling,
        audit_verdict=verdict,
        audit_reasoning="x",
        system2_output={"proposed_action": "do it"},
    )

    row = stage_action.run(eid, _build_row(eid, scope=scope_name, ceiling=ceiling, verdict=verdict))

    assert row["action_taken"] == expected_action
    assert row["effective_autonomy"] == expected_eff
    assert row["status"] == "complete"

    persisted = read_event(eid)
    assert persisted["action_taken"] == expected_action
    assert persisted["effective_autonomy"] == expected_eff
    assert persisted["status"] == "complete"


def test_stage_action_creates_decisions_mirror(solomon_db, monkeypatch):
    """stage_action.run also mirrors the events row into decisions."""
    reset_tenant_cache()
    from solomon.storage import decisions
    decisions.get_or_create_tenant_id()

    _set_scope_autonomy("ops", "L3")
    seed_event(
        event_id="ev-mirror-after-action",
        classification={"scope": "ops", "domain": "general", "decision_type": "reply", "confidence": 0.6},
        owner_state_ceiling=4,
        audit_verdict="APPROVE",
        audit_reasoning="ok",
        system2_output={"proposed_action": "reply now", "reasoning": "x", "confidence": 0.6},
    )

    stage_action.run("ev-mirror-after-action", _build_row(
        "ev-mirror-after-action", scope="ops", ceiling=4, verdict="APPROVE"
    ))

    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT COUNT(*) FROM decisions WHERE event_id = ?", ("ev-mirror-after-action",))
            count = cur.fetchone()[0]
    assert count == 1

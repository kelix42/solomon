"""Test for solomon.pipeline.stage_audit."""

from __future__ import annotations

import json

import pytest

from solomon.pipeline import stage_audit

from tests._pipeline_helpers import install_stub_llm, read_event, seed_event


def _seed_with_system2(event_id, proposed_action="Send the quote"):
    seed_event(
        event_id=event_id,
        classification={"scope": "pricing", "domain": "commercial"},
        system1_output={"answer": "ship", "confidence": 0.5, "scope": "pricing"},
        system2_output={"reasoning": "ok", "proposed_action": proposed_action, "confidence": 0.7},
        divergence_score=0.2,
    )


def _audit_response(verdict, reasoning="ok"):
    return json.dumps({
        "verdict": verdict,
        "reasoning": reasoning,
        "checks_passed": ["coherence", "tone"],
        "checks_failed": [],
    })


@pytest.mark.parametrize("raw_verdict,expected", [
    ("approve", "APPROVE"),
    ("reject", "REJECT"),
    ("request_rethink", "REQUEST_RETHINK"),
])
def test_stage_audit_three_verdicts(solomon_db, monkeypatch, raw_verdict, expected):
    """Each of the three Session-A verdicts round-trips correctly."""
    stub = install_stub_llm(monkeypatch)
    stub.add(
        lambda kw: "audit gate" in kw["system"].lower(),
        lambda kw: _audit_response(raw_verdict, reasoning=f"stub said {raw_verdict}"),
    )

    eid = f"ev-audit-{raw_verdict}"
    _seed_with_system2(eid)

    row = stage_audit.run(eid, {
        "event_id": eid,
        "tenant_id": "default",
        "raw_content": "x",
        "classification": {"scope": "pricing"},
        "system2_output": {"reasoning": "ok", "proposed_action": "Send the quote", "confidence": 0.7},
        "divergence_score": 0.2,
    })

    assert row["audit_verdict"] == expected
    persisted = read_event(eid)
    assert persisted["audit_verdict"] == expected
    assert persisted["audit_reasoning"] is not None

    # tier="deep"
    assert any(c["tier"] == "deep" for c in stub.calls)


def test_stage_audit_downgrade_collapses_to_approve(solomon_db, monkeypatch):
    """AuditGate's 'downgrade' verdict normalises to APPROVE (per session prompt)."""
    stub = install_stub_llm(monkeypatch)
    stub.add(
        lambda kw: "audit gate" in kw["system"].lower(),
        lambda kw: _audit_response("downgrade", reasoning="lower the autonomy a notch"),
    )

    _seed_with_system2("ev-audit-down")
    row = stage_audit.run("ev-audit-down", {
        "event_id": "ev-audit-down",
        "tenant_id": "default",
        "raw_content": "x",
        "classification": {"scope": "ops"},
        "system2_output": {"reasoning": "ok", "proposed_action": "do it", "confidence": 0.6},
        "divergence_score": 0.1,
    })
    assert row["audit_verdict"] == "APPROVE"

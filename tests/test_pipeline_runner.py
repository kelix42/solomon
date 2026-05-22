"""Integration test for solomon.pipeline.runner — the 10-stage walker.

Covers:
  - Happy path: all 10 stages run, status='complete', timings recorded.
  - Halt-on-skipped: low salience → status='skipped', later stages don't fire.
  - Halt-on-blocked: hard-rule match → status='blocked_by_hard_rule', stops.
  - Timings dict is well-formed JSON with one key per stage that ran.

The LLM is stubbed via system-prompt routing (pattern from
``tests/test_session_runner.py::_StubLLMClient``). One shared stub handles
salience / classification / system1 / system2 / audit.
"""

from __future__ import annotations

import json
from typing import Optional

import pytest

from solomon.pipeline import runner
from solomon.storage.pool import cursor, execute, get_conn, parse_json

from tests._pipeline_helpers import (
    install_stub_llm,
    read_event,
    reset_tenant_cache,
    seed_event,
)


# ---------------------------------------------------------------------------
# A single stub LLM that responds to every tier/system prompt the pipeline
# might fire during a happy-path run.
# ---------------------------------------------------------------------------

def _install_full_stub(monkeypatch, *, salience_score: float = 0.8):
    stub = install_stub_llm(monkeypatch)

    # Stage 2: salience scorer — system prompt mentions "salience"-y words.
    def _salience_resp(kw):
        return json.dumps({
            "stakes": salience_score,
            "novelty": salience_score,
            "emotion": salience_score,
            "owner_involvement": salience_score,
        })

    stub.add(
        lambda kw: "rate how much" in kw["system"].lower(),
        _salience_resp,
    )

    # Stage 3: classification.
    stub.add(
        lambda kw: "classify" in kw["system"].lower() or "scope" in kw["system"].lower() and "taxonomy" in kw["system"].lower(),
        json.dumps({
            "scope": "pricing", "domain": "commercial",
            "decision_type": "quote", "confidence": 0.78,
        }),
    )

    # Stage 6: System 1.
    stub.add(
        lambda kw: "System 1" in kw["system"],
        lambda kw: "Ship at 18% margin — above floor.",
    )

    # Stage 7: System 2 (JSON).
    stub.add(
        lambda kw: "System 2" in kw["system"],
        lambda kw: json.dumps({
            "reasoning": "Margin 18% > 15% floor; no other red flags.",
            "proposed_action": "Send the quote at $4,200.",
            "confidence": 0.82,
        }),
    )

    # Stage 8: audit gate.
    stub.add(
        lambda kw: "audit gate" in kw["system"].lower(),
        lambda kw: json.dumps({
            "verdict": "approve",
            "reasoning": "passes all checks",
            "checks_passed": ["coherence"],
            "checks_failed": [],
        }),
    )

    return stub


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_runner_happy_path_all_stages(solomon_db, monkeypatch):
    """All 10 stages run; status='complete'; action_taken populated."""
    reset_tenant_cache()
    from solomon.storage import decisions
    decisions.get_or_create_tenant_id()

    _install_full_stub(monkeypatch, salience_score=0.85)

    # Seed a pending event row.
    seed_event(event_id="ev-happy", status="pending")

    result = runner.run("ev-happy")
    assert result is not None
    assert result["status"] == "complete"
    assert result["action_taken"] in {"ship", "one-tap", "suggest", "escalate"}
    assert result["effective_autonomy"] is not None
    # Approve verdict → not escalate.
    assert result["audit_verdict"] == "APPROVE"
    assert result["action_taken"] != "escalate"

    # Mirror to decisions table happened.
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT COUNT(*) FROM decisions WHERE event_id = ?", ("ev-happy",))
            assert cur.fetchone()[0] == 1


def test_runner_happy_path_timings_well_formed(solomon_db, monkeypatch):
    """stage_timings_ms holds one int per stage that ran."""
    reset_tenant_cache()
    from solomon.storage import decisions
    decisions.get_or_create_tenant_id()

    _install_full_stub(monkeypatch, salience_score=0.85)
    seed_event(event_id="ev-timings", status="pending")

    result = runner.run("ev-timings")
    assert result is not None

    timings = result.get("stage_timings_ms")
    if isinstance(timings, str):
        timings = parse_json(timings)
    assert isinstance(timings, dict)

    # All ten stages should have produced a timing key.
    expected = {
        "capture", "salience", "classification", "hard_rule",
        "retrieval", "system1", "system2", "audit", "owner_state", "action",
    }
    present = {k for k in timings.keys() if not k.startswith("_")}
    missing = expected - present
    assert not missing, f"missing timings for: {missing} (got {present})"

    # And each is a non-negative integer (ms).
    for k in expected:
        v = timings[k]
        assert isinstance(v, int)
        assert v >= 0


# ---------------------------------------------------------------------------
# Halt-on-skipped (low salience)
# ---------------------------------------------------------------------------

def test_runner_halts_on_low_salience(solomon_db, monkeypatch):
    """Salience < 0.30 → status='skipped'; later stages don't fire."""
    reset_tenant_cache()
    from solomon.storage import decisions
    decisions.get_or_create_tenant_id()

    stub = _install_full_stub(monkeypatch, salience_score=0.05)
    seed_event(event_id="ev-low", status="pending")

    result = runner.run("ev-low")
    assert result is not None
    assert result["status"] == "skipped"
    # Nothing past stage 2 should have fired → no system1/2/audit columns.
    persisted = read_event("ev-low")
    assert persisted["system1_output"] is None
    assert persisted["system2_output"] is None
    assert persisted["audit_verdict"] is None
    assert persisted["action_taken"] is None

    # Stub never received a System 1 / System 2 / audit prompt.
    seen_systems = {c["system"] for c in stub.calls}
    assert not any("System 1" in s for s in seen_systems)
    assert not any("System 2" in s for s in seen_systems)
    assert not any("audit gate" in s.lower() for s in seen_systems)


# ---------------------------------------------------------------------------
# Halt-on-blocked (hard-rule match)
# ---------------------------------------------------------------------------

def test_runner_halts_on_hard_rule(solomon_db, monkeypatch, tmp_path):
    """A matching JSON-logic rule → status='blocked_by_hard_rule'."""
    reset_tenant_cache()
    from solomon.storage import decisions
    decisions.get_or_create_tenant_id()

    # Write a foundation file with one always-true rule, then point the
    # stage at it via env var.
    foundation = tmp_path / "non_negotiables.yaml"
    foundation.write_text(
        "rules:\n"
        "  - id: always-block\n"
        "    statement: blocks everything for this test\n"
        "    condition: true\n"
        "    on_violate:\n"
        "      explanation: blocks because tests said so\n"
    )
    monkeypatch.setenv("SOLOMON_NON_NEGOTIABLES_PATH", str(foundation))

    stub = _install_full_stub(monkeypatch, salience_score=0.85)
    seed_event(event_id="ev-blocked", status="pending")

    result = runner.run("ev-blocked")
    assert result is not None
    assert result["status"] == "blocked_by_hard_rule"
    # System 1 / System 2 / audit / action should NOT have run.
    persisted = read_event("ev-blocked")
    assert persisted["system1_output"] is None
    assert persisted["system2_output"] is None
    assert persisted["audit_verdict"] is None
    assert persisted["action_taken"] is None

    # No System 1 / 2 / audit prompts fired.
    seen_systems = {c["system"] for c in stub.calls}
    assert not any("System 1" in s for s in seen_systems)
    assert not any("System 2" in s for s in seen_systems)


# ---------------------------------------------------------------------------
# Missing event
# ---------------------------------------------------------------------------

def test_runner_returns_none_for_missing_event(solomon_db, monkeypatch):
    """No events row → runner returns None, no exception."""
    reset_tenant_cache()
    from solomon.storage import decisions
    decisions.get_or_create_tenant_id()
    install_stub_llm(monkeypatch)

    result = runner.run("does-not-exist")
    assert result is None

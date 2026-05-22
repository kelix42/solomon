"""Test for solomon.pipeline.stage_system2 (+ inline divergence)."""

from __future__ import annotations

import json

import pytest

from solomon.pipeline import stage_system2
from solomon.storage.pool import parse_json

from tests._pipeline_helpers import install_stub_llm, read_event, seed_event


def test_stage_system2_writes_outputs_and_divergence(solomon_db, monkeypatch):
    """system2_output JSON + divergence_score float are persisted."""
    stub = install_stub_llm(monkeypatch)

    def _s2_resp(kw):
        return json.dumps({
            "reasoning": "Margin is 18%, above the 15% floor, no red flags in retrieval.",
            "proposed_action": "Send quote at $4,200 / 18% margin.",
            "confidence": 0.82,
        })

    stub.add(lambda kw: "System 2" in kw["system"], _s2_resp)

    seed_event(
        event_id="ev-s2-1",
        classification={"scope": "pricing", "domain": "commercial"},
        retrieval_context={"lanes_used": ["semantic"], "heuristic_ids": ["h1"]},
        system1_output={"answer": "Ship at 18%.", "confidence": 0.5, "scope": "pricing"},
    )

    row = stage_system2.run("ev-s2-1", {
        "event_id": "ev-s2-1",
        "tenant_id": "default",
        "raw_content": "quote at 18% margin?",
        "classification": {"scope": "pricing"},
        "retrieval_context": {"lanes_used": ["semantic"], "heuristic_ids": ["h1"]},
        "system1_output": {"answer": "Ship at 18%.", "confidence": 0.5, "scope": "pricing"},
    })

    assert row["system2_output"]["proposed_action"] == "Send quote at $4,200 / 18% margin."
    assert row["system2_output"]["confidence"] == pytest.approx(0.82)
    div = row["divergence_score"]
    assert isinstance(div, float)
    assert 0.0 <= div <= 1.0

    persisted = read_event("ev-s2-1")
    parsed = parse_json(persisted["system2_output"])
    assert parsed["proposed_action"] == "Send quote at $4,200 / 18% margin."
    assert persisted["divergence_score"] is not None
    assert 0.0 <= persisted["divergence_score"] <= 1.0

    # tier="deep"
    assert len(stub.calls) == 1
    assert stub.calls[0]["tier"] == "deep"


def test_stage_system2_handles_malformed_json(solomon_db, monkeypatch):
    """Non-JSON model output is captured as ``reasoning`` text; no crash."""
    stub = install_stub_llm(monkeypatch)
    stub.add(
        lambda kw: "System 2" in kw["system"],
        "raw text without braces",
    )

    seed_event(
        event_id="ev-s2-mal",
        classification={"scope": "ops"},
        system1_output={"answer": "ok", "confidence": 0.5, "scope": "ops"},
    )

    row = stage_system2.run("ev-s2-mal", {
        "event_id": "ev-s2-mal",
        "tenant_id": "default",
        "raw_content": "x",
        "classification": {"scope": "ops"},
        "retrieval_context": None,
        "system1_output": {"answer": "ok", "confidence": 0.5, "scope": "ops"},
    })

    # Reasoning fell back to the raw text; divergence is still in [0, 1].
    assert "raw text" in row["system2_output"]["reasoning"]
    assert isinstance(row["divergence_score"], float)

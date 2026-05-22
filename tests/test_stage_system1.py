"""Test for solomon.pipeline.stage_system1."""

from __future__ import annotations

import json

import pytest

from solomon.pipeline import stage_system1
from solomon.storage.pool import parse_json

from tests._pipeline_helpers import StubLLM, install_stub_llm, read_event, seed_event


def test_stage_system1_writes_payload(solomon_db, monkeypatch):
    """Happy path — system1_output column is populated with the JSON answer."""
    stub = install_stub_llm(monkeypatch)
    stub.add(
        lambda kw: "System 1" in kw["system"],
        lambda kw: "Ship at 18% — well above floor.",
    )

    seed_event(
        event_id="ev-s1-1",
        classification={"scope": "pricing", "domain": "commercial", "decision_type": "quote", "confidence": 0.7},
        retrieval_context={"heuristic_ids": ["h-floor-15", "h-margin-default"]},
    )

    row = stage_system1.run("ev-s1-1", {
        "event_id": "ev-s1-1",
        "tenant_id": "default",
        "source": "telegram",
        "received_at": None,
        "raw_content": "quote at 18% margin?",
        "classification": {"scope": "pricing"},
        "retrieval_context": {"heuristic_ids": ["h-floor-15"]},
    })

    assert row["system1_output"]["answer"] == "Ship at 18% — well above floor."

    persisted = read_event("ev-s1-1")
    parsed = parse_json(persisted["system1_output"])
    assert isinstance(parsed, dict)
    assert parsed["answer"] == "Ship at 18% — well above floor."

    # And the LLM was called once on tier="fast".
    assert len(stub.calls) == 1
    assert stub.calls[0]["tier"] == "fast"


def test_stage_system1_handles_unconfigured(solomon_db, monkeypatch):
    """An unconfigured client → empty answer, no crash, column still written."""

    class _Unc:
        configured = False

        def call(self, **kwargs):
            raise AssertionError("should not be reached")

        @staticmethod
        def parse_json(text):
            return None

    install_stub_llm(monkeypatch, _Unc())  # type: ignore[arg-type]

    seed_event(event_id="ev-s1-unc")
    row = stage_system1.run("ev-s1-unc", {
        "event_id": "ev-s1-unc",
        "tenant_id": "default",
        "source": "telegram",
        "raw_content": "x",
        "classification": None,
        "retrieval_context": None,
    })

    assert row["system1_output"]["answer"] == ""
    persisted = read_event("ev-s1-unc")
    parsed = parse_json(persisted["system1_output"])
    assert parsed["answer"] == ""

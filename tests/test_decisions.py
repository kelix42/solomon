"""Tests for solomon.storage.decisions — the rewritten pool-API version.

The legacy module used ``%s`` placeholders directly and crashed on
SQLite. This test file proves the rewrite works on SQLite (the default
backend) via the ``solomon_db`` fixture, and exercises every public
function.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from solomon.storage import decisions as dec
from solomon.storage.pool import cursor, execute, get_conn


# ---------------------------------------------------------------------------
# Helpers — insert an events row directly via the pool for round-trip tests.
# ---------------------------------------------------------------------------

def _insert_event(
    *,
    event_id: str,
    tenant_id: str = "default",
    salience_score: float | None = 0.8,
    classification: dict | None = None,
    retrieval_context: dict | None = None,
    system1_output: str | None = None,
    system2_output: str | None = None,
    divergence_score: float | None = None,
    audit_verdict: str | None = None,
    audit_reasoning: str | None = None,
    owner_state: str | None = None,
    owner_state_ceiling: int | None = None,
    effective_autonomy: int | None = None,
    action_taken: str | None = None,
    status: str = "complete",
) -> None:
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO events ("
                "  event_id, tenant_id, source, received_at, participants, "
                "  raw_content, channel_metadata, salience_score, classification, "
                "  retrieval_context, system1_output, system2_output, "
                "  divergence_score, audit_verdict, audit_reasoning, "
                "  owner_state, owner_state_ceiling, effective_autonomy, "
                "  action_taken, status"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id, tenant_id, "telegram",
                    datetime.now(timezone.utc).isoformat(),
                    "[]", "the owner said something", "{}",
                    salience_score,
                    json.dumps(classification) if classification is not None else None,
                    json.dumps(retrieval_context) if retrieval_context is not None else None,
                    system1_output, system2_output,
                    divergence_score, audit_verdict, audit_reasoning,
                    owner_state, owner_state_ceiling, effective_autonomy,
                    action_taken, status,
                ),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# get_or_create_tenant_id
# ---------------------------------------------------------------------------

def test_get_or_create_tenant_id_default(solomon_db, monkeypatch):
    """Returns 'default' for a single-tenant install and the tenant row exists."""
    monkeypatch.delenv("SOLOMON_TENANT_ID", raising=False)
    dec.reset_tenant_cache()

    tid = dec.get_or_create_tenant_id()
    assert tid == "default"

    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT tenant_id FROM tenants WHERE tenant_id = ?", ("default",))
            row = cur.fetchone()
    assert row is not None


def test_get_or_create_tenant_id_custom(solomon_db, monkeypatch):
    """Honours SOLOMON_TENANT_ID and creates the row."""
    monkeypatch.setenv("SOLOMON_TENANT_ID", "kekeli-co")
    monkeypatch.setenv("SOLOMON_BUSINESS_NAME", "Kekeli & Co")
    dec.reset_tenant_cache()

    tid = dec.get_or_create_tenant_id()
    assert tid == "kekeli-co"

    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT business_name FROM tenants WHERE tenant_id = ?", ("kekeli-co",))
            row = cur.fetchone()
    assert row is not None
    assert row[0] == "Kekeli & Co"


def test_get_or_create_tenant_id_idempotent(solomon_db, monkeypatch):
    """Calling twice doesn't double-insert; the cache short-circuits."""
    monkeypatch.delenv("SOLOMON_TENANT_ID", raising=False)
    dec.reset_tenant_cache()

    dec.get_or_create_tenant_id()
    dec.get_or_create_tenant_id()
    dec.get_or_create_tenant_id()

    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT COUNT(*) FROM tenants WHERE tenant_id = ?", ("default",))
            row = cur.fetchone()
    assert row[0] == 1


# ---------------------------------------------------------------------------
# mirror_event_to_decision
# ---------------------------------------------------------------------------

def test_mirror_event_to_decision_copies_columns(solomon_db, monkeypatch):
    """Happy path — every important field lands in the decisions row."""
    monkeypatch.delenv("SOLOMON_TENANT_ID", raising=False)
    dec.reset_tenant_cache()
    dec.get_or_create_tenant_id()

    _insert_event(
        event_id="ev-mirror-1",
        salience_score=0.72,
        classification={"scope": "pricing", "domain": "commercial", "decision_type": "quote", "confidence": 0.83},
        retrieval_context={
            "working_memory_used": True,
            "lanes_used": ["semantic", "recency"],
            "heuristic_ids": ["h1", "h2"],
            "foundation_files": ["00-industry.yaml"],
        },
        system1_output="ship at 18% margin",
        system2_output=json.dumps({"reasoning": "...", "proposed_action": "Quote $4,200", "confidence": 0.7}),
        divergence_score=0.42,
        audit_verdict="approve",
        audit_reasoning="passes all five checks",
        owner_state="green",
        owner_state_ceiling=4,
        effective_autonomy=3,
        action_taken="ship",
    )

    decision_id = dec.mirror_event_to_decision("ev-mirror-1")
    assert decision_id is not None and decision_id > 0

    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,))
            row = dict(zip([d[0] for d in cur.description], cur.fetchone()))

    assert row["event_id"] == "ev-mirror-1"
    assert row["scope"] == "pricing"
    assert row["domain"] == "commercial"
    assert row["decision_type"] == "quote"
    assert row["classification_confidence"] == pytest.approx(0.83)
    assert row["salience_score"] == pytest.approx(0.72)
    assert row["working_memory_used"] == 1
    assert row["divergence_score"] == pytest.approx(0.42)
    assert row["proposed_action"] == "Quote $4,200"
    assert row["audit_verdict"] == "approve"
    assert row["audit_reasoning"] == "passes all five checks"
    assert row["final_action"] == "ship"
    assert row["autonomy_level_at_time"] == "L3"

    # Audit log row should mirror too.
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT verdict, reasoning FROM audit_log WHERE decision_id = ?", (decision_id,))
            audit_row = cur.fetchone()
    assert audit_row is not None
    assert audit_row[0] == "approve"


def test_mirror_event_to_decision_missing_event(solomon_db, monkeypatch):
    """Missing event_id returns None, doesn't raise."""
    monkeypatch.delenv("SOLOMON_TENANT_ID", raising=False)
    dec.reset_tenant_cache()
    dec.get_or_create_tenant_id()

    result = dec.mirror_event_to_decision("does-not-exist")
    assert result is None


def test_mirror_event_to_decision_minimal(solomon_db, monkeypatch):
    """An events row with mostly NULLs still produces a decisions row."""
    monkeypatch.delenv("SOLOMON_TENANT_ID", raising=False)
    dec.reset_tenant_cache()
    dec.get_or_create_tenant_id()

    _insert_event(event_id="ev-min", classification=None, retrieval_context=None)

    decision_id = dec.mirror_event_to_decision("ev-min")
    assert decision_id is not None and decision_id > 0

    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT event_id, scope, audit_verdict FROM decisions WHERE decision_id = ?", (decision_id,))
            row = cur.fetchone()
    assert row[0] == "ev-min"
    assert row[1] is None
    assert row[2] is None


# ---------------------------------------------------------------------------
# DecisionLog.log (the conductor's path)
# ---------------------------------------------------------------------------

class _StubTurn:
    """Minimal TurnContext-shaped object for DecisionLog.log()."""

    def __init__(self, **overrides):
        defaults = dict(
            raw_event=None,
            scope="ops",
            domain="general",
            decision_type="reply",
            classification_confidence=0.6,
            salience_score=0.5,
            working_memory_used=False,
            retrieval_lanes_used=[],
            heuristics_referenced=[],
            foundation_files_used=[],
            system_1_answer="quick",
            system_2_answer="slow",
            divergence_score=0.1,
            proposed_action="reply",
            audit_verdict="approve",
            audit_reasoning="ok",
            final_action="ship",
            autonomy_level_at_time="L1",
        )
        defaults.update(overrides)
        for k, v in defaults.items():
            setattr(self, k, v)


def test_decision_log_log_roundtrip(solomon_db, monkeypatch):
    """DecisionLog.log() writes the row and returns the new decision_id."""
    monkeypatch.delenv("SOLOMON_TENANT_ID", raising=False)
    dec.reset_tenant_cache()
    dec.get_or_create_tenant_id()

    log = dec.DecisionLog(adapter=None)
    turn = _StubTurn()
    decision_id = log.log(turn)
    assert decision_id > 0

    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT scope, audit_verdict, final_action FROM decisions WHERE decision_id = ?",
                (decision_id,),
            )
            row = cur.fetchone()
    assert row[0] == "ops"
    assert row[1] == "approve"
    assert row[2] == "ship"


def test_decision_log_log_with_raw_event(solomon_db, monkeypatch):
    """Passing a raw_event upserts the events row before inserting the decision."""
    from solomon.capture.raw_event import RawEvent

    monkeypatch.delenv("SOLOMON_TENANT_ID", raising=False)
    dec.reset_tenant_cache()
    dec.get_or_create_tenant_id()

    raw = RawEvent(
        id="ev-roundtrip-1",
        source="telegram",
        received_at=datetime.now(timezone.utc),
        participants=["kekeli"],
        raw_content="hey solomon",
        channel_metadata={"chat_id": 1515920282},
    )
    turn = _StubTurn(raw_event=raw, salience_score=0.7)

    log = dec.DecisionLog(adapter=None)
    decision_id = log.log(turn)
    assert decision_id > 0

    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT event_id, salience_score FROM events WHERE event_id = ?",
                    ("ev-roundtrip-1",))
            ev_row = cur.fetchone()
            execute(cur, "SELECT event_id FROM decisions WHERE decision_id = ?", (decision_id,))
            d_row = cur.fetchone()
    assert ev_row is not None
    assert ev_row[0] == "ev-roundtrip-1"
    assert d_row[0] == "ev-roundtrip-1"

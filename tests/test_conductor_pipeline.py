"""Tests for the Session B pipeline wire-up inside solomon/conductor.py.

Covers _pre_llm_call's new behaviour:

  * Kill switch SOLOMON_PIPELINE_DISABLE=1 → legacy body runs, no events row.
  * Inline mode happy path → events row + TurnContext populated + verdict
    routing.
  * Inline mode low salience (status='skipped') → no system message.
  * Inline mode blocked-by-hard-rule → decline message injected.
  * Inline mode audit REJECT → decline message injected.
  * Inline mode audit REQUEST_RETHINK → rethink message injected.
  * Queue mode → events row 'pending', no run() call.
  * Pipeline runner raises → caught, status='errored', legacy path runs.

The runner is always stubbed (its stages are tested in session A); we
write the events-row mutations these tests need inside the stub so the
read-back exercises the real DB plumbing.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest

from solomon.capture.raw_event import RawEvent
from solomon import conductor as conductor_mod
from solomon.conductor import Conductor, TurnContext
from solomon.storage import decisions as dec
from solomon.storage.pool import cursor, execute, get_conn


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


class _NoopPrivateMode:
    """Stub PrivateMode that is never active."""

    def is_active(self, session_id: str) -> bool:  # noqa: ANN001
        return False

    def on_session_start(self, session_id: str) -> None:  # noqa: ANN001
        return None

    def on_session_end(self, session_id: str) -> None:  # noqa: ANN001
        return None

    def record_private_turn(self, session_id: str) -> None:  # noqa: ANN001
        return None


def _make_conductor() -> Conductor:
    """Build a Conductor without running its heavy lazy-imports __init__.

    The pipeline path only touches ``self.private_mode`` and
    ``self._turns``. Legacy-fallback tests install component stubs onto
    the instance as needed.
    """
    c = Conductor.__new__(Conductor)
    c.adapter = None  # type: ignore[assignment]
    c.private_mode = _NoopPrivateMode()  # type: ignore[assignment]
    c._turns = {}
    return c


def _make_raw_event(text: str = "should I quote at 18% margin") -> RawEvent:
    return RawEvent(
        id="msg-1",
        source="telegram",
        received_at=datetime.now(timezone.utc),
        participants=["kekeli"],
        raw_content=text,
        channel_metadata={"chat_id": 1515920282, "session_id": "s1"},
    )


def _count_events() -> int:
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT COUNT(*) FROM events", ())
            return cur.fetchone()[0]


def _read_event(event_id: str) -> Dict[str, Any]:
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT * FROM events WHERE event_id = ?", (event_id,))
            row = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], row))


def _install_pipeline_stub(monkeypatch, mutate_event=None, raise_with=None):
    """Replace solomon.pipeline.runner.run with a stub.

    ``mutate_event``: callable(event_id) → updates the events row to
    simulate what the real runner would write (status, verdict, etc.).
    ``raise_with``: if set, the stub raises this exception instead.

    Returns a dict tracking call count.
    """
    calls: Dict[str, int] = {"count": 0}

    def _stub_run(event_id: str, **kwargs):  # noqa: ANN001
        calls["count"] += 1
        if raise_with is not None:
            raise raise_with
        if mutate_event is not None:
            mutate_event(event_id)
        return None

    from solomon.pipeline import runner as runner_mod
    monkeypatch.setattr(runner_mod, "run", _stub_run)
    # The conductor imports `run` inside the function body via
    # `from .pipeline.runner import run as run_pipeline`, so patching
    # the module attribute is enough.

    return calls


def _update_event(event_id: str, **fields):
    """Helper for stubs that need to write events-row columns."""
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    params = list(fields.values()) + [event_id]
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, f"UPDATE events SET {cols} WHERE event_id = ?", tuple(params))
        conn.commit()


@pytest.fixture(autouse=True)
def _reset_tenant_cache():
    """Clear the cached tenant_id between tests so each test's solomon_db
    fixture starts fresh."""
    dec.reset_tenant_cache()
    yield
    dec.reset_tenant_cache()


# ---------------------------------------------------------------------------
# Env-var helpers (cheap unit tests for the kill-switch parser)
# ---------------------------------------------------------------------------


def test_pipeline_disabled_truthy(monkeypatch):
    for v in ("1", "true", "TRUE", "yes", "on", " true "):
        monkeypatch.setenv("SOLOMON_PIPELINE_DISABLE", v)
        assert conductor_mod._pipeline_disabled() is True


def test_pipeline_disabled_falsy(monkeypatch):
    monkeypatch.delenv("SOLOMON_PIPELINE_DISABLE", raising=False)
    assert conductor_mod._pipeline_disabled() is False
    for v in ("0", "false", "no", "", "anything"):
        monkeypatch.setenv("SOLOMON_PIPELINE_DISABLE", v)
        assert conductor_mod._pipeline_disabled() is False


def test_pipeline_mode_defaults_to_inline(monkeypatch):
    monkeypatch.delenv("SOLOMON_PIPELINE_MODE", raising=False)
    assert conductor_mod._pipeline_mode() == "inline"
    monkeypatch.setenv("SOLOMON_PIPELINE_MODE", "")
    assert conductor_mod._pipeline_mode() == "inline"
    monkeypatch.setenv("SOLOMON_PIPELINE_MODE", "QUEUE")
    assert conductor_mod._pipeline_mode() == "queue"
    monkeypatch.setenv("SOLOMON_PIPELINE_MODE", "wat")
    assert conductor_mod._pipeline_mode() == "inline"


# ---------------------------------------------------------------------------
# Kill switch: pipeline disabled → legacy path, no events row
# ---------------------------------------------------------------------------


def test_kill_switch_runs_legacy_path_and_skips_events_insert(
    solomon_db, monkeypatch
):
    """SOLOMON_PIPELINE_DISABLE=1 → legacy body runs, no events row inserted
    by the conductor, no pipeline runner call."""
    monkeypatch.setenv("SOLOMON_PIPELINE_DISABLE", "1")
    monkeypatch.delenv("SOLOMON_TENANT_ID", raising=False)
    calls = _install_pipeline_stub(monkeypatch)

    # Stub the legacy components — we only assert they were touched.
    legacy_hits = {"classify": 0}

    class _Sal:
        def score(self, raw_event, scope=None):
            legacy_hits["classify"] += 1

            class _R:
                score = 0.5
                breakdown = {"x": 1.0}

            return _R()

    class _Cls:
        def classify(self, raw_event):
            legacy_hits["classify"] += 1

            class _R:
                scope = "ops"
                domain = "general"
                decision_type = "reply"
                confidence = 0.7

            return _R()

    class _NN:
        def check(self, raw_event, scope=None):
            return None

    class _Hot:
        def is_thin(self):
            return True

    class _WM:
        def fetch(self, scope=None, raw_event=None):
            return _Hot()

        def update_after_turn(self, turn):
            return None

    class _Ret:
        def retrieve(self, raw_event, scope=None, domain=None):
            class _R:
                lanes = ["semantic"]
                heuristic_ids = ["h1"]
                foundation_files = []

            return _R()

    class _S1:
        def predict(self, raw_event, scope=None, heuristics=None):
            class _R:
                answer = "go"

            return _R()

    class _S2:
        def reason(self, raw_event, scope=None, context=None, heuristic_ids=None):
            class _R:
                answer = "go slowly"
                proposed_action = "ship"

            return _R()

    class _AG:
        def run(self, **kwargs):
            class _R:
                verdict = "approve"
                reasoning = "ok"

            return _R()

    class _AL:
        def level_for(self, scope):
            return "L3"

    c = _make_conductor()
    c.classifier = _Cls()
    c.salience = _Sal()
    c.non_negotiables = _NN()
    c.working_memory = _WM()
    c.retrieval = _Ret()
    c.s1 = _S1()
    c.s2 = _S2()
    c._divergence = lambda a, b: 0.1
    c.audit_gate = _AG()
    c.autonomy = _AL()

    turn_ctx = TurnContext(raw_event=_make_raw_event())
    c._turns["s1"] = turn_ctx

    messages: list = []
    c._pre_llm_call(session_id="s1", messages=messages)

    # Legacy populated the turn.
    assert turn_ctx.scope == "ops"
    assert turn_ctx.audit_verdict == "approve"
    assert turn_ctx.event_id is None

    # No events row inserted (legacy doesn't touch events at pre_llm_call).
    assert _count_events() == 0
    # No system message injected.
    assert messages == []
    # Pipeline runner stub never called.
    assert calls["count"] == 0


# ---------------------------------------------------------------------------
# Inline mode happy path
# ---------------------------------------------------------------------------


def test_inline_mode_happy_path_populates_turn_and_creates_events_row(
    solomon_db, monkeypatch
):
    """Inline mode + complete + APPROVE → events row created, 14 columns
    populated onto TurnContext, no system message injected."""
    monkeypatch.delenv("SOLOMON_PIPELINE_DISABLE", raising=False)
    monkeypatch.delenv("SOLOMON_PIPELINE_MODE", raising=False)
    monkeypatch.delenv("SOLOMON_TENANT_ID", raising=False)

    def mutate(event_id: str):
        _update_event(
            event_id,
            salience_score=0.72,
            classification=json.dumps(
                {"scope": "pricing", "domain": "commercial",
                 "decision_type": "quote", "confidence": 0.83}
            ),
            system1_output=json.dumps(
                {"answer": "yes ship", "confidence": 0.7, "scope": "pricing"}
            ),
            system2_output=json.dumps(
                {"reasoning": "margin healthy", "proposed_action": "Quote $4,200",
                 "confidence": 0.7}
            ),
            divergence_score=0.31,
            audit_verdict="APPROVE",
            audit_reasoning="passes",
            owner_state="green",
            owner_state_ceiling=4,
            effective_autonomy=3,
            action_taken="one-tap",
            stage_timings_ms=json.dumps({"salience": 12, "audit": 88}),
            status="complete",
        )

    calls = _install_pipeline_stub(monkeypatch, mutate_event=mutate)

    c = _make_conductor()
    turn_ctx = TurnContext(raw_event=_make_raw_event())
    c._turns["s1"] = turn_ctx

    messages: list = []
    c._pre_llm_call(session_id="s1", messages=messages)

    # Pipeline ran exactly once.
    assert calls["count"] == 1
    # An events row was created by the conductor.
    assert _count_events() == 1

    # The 14 columns landed on the TurnContext.
    assert turn_ctx.event_id is not None
    assert turn_ctx.salience_score == pytest.approx(0.72)
    assert turn_ctx.classification == {
        "scope": "pricing", "domain": "commercial",
        "decision_type": "quote", "confidence": 0.83,
    }
    assert turn_ctx.scope == "pricing"
    assert turn_ctx.domain == "commercial"
    assert turn_ctx.decision_type == "quote"
    assert turn_ctx.classification_confidence == pytest.approx(0.83)
    assert isinstance(turn_ctx.system1_output, dict)
    assert turn_ctx.system1_output["answer"] == "yes ship"
    assert isinstance(turn_ctx.system2_output, dict)
    assert turn_ctx.system2_output["proposed_action"] == "Quote $4,200"
    assert turn_ctx.divergence_score == pytest.approx(0.31)
    assert turn_ctx.audit_verdict == "APPROVE"
    assert turn_ctx.audit_reasoning == "passes"
    assert turn_ctx.owner_state == "green"
    assert turn_ctx.owner_state_ceiling == 4
    assert turn_ctx.effective_autonomy == 3
    assert turn_ctx.action_taken == "one-tap"
    assert turn_ctx.stage_timings_ms == {"salience": 12, "audit": 88}
    assert turn_ctx.status == "complete"
    # autonomy_level_at_time mirrors effective_autonomy.
    assert turn_ctx.autonomy_level_at_time == "L3"

    # APPROVE → no system-message injection.
    assert messages == []


# ---------------------------------------------------------------------------
# Inline mode low salience → status='skipped', no system message
# ---------------------------------------------------------------------------


def test_inline_mode_low_salience_skipped_injects_nothing(solomon_db, monkeypatch):
    monkeypatch.delenv("SOLOMON_PIPELINE_DISABLE", raising=False)
    monkeypatch.delenv("SOLOMON_PIPELINE_MODE", raising=False)

    def mutate(event_id):
        _update_event(event_id, salience_score=0.05, status="skipped")

    _install_pipeline_stub(monkeypatch, mutate_event=mutate)

    c = _make_conductor()
    turn_ctx = TurnContext(raw_event=_make_raw_event("hi"))
    c._turns["s1"] = turn_ctx

    messages: list = []
    c._pre_llm_call(session_id="s1", messages=messages)

    assert turn_ctx.status == "skipped"
    assert turn_ctx.audit_verdict is None
    assert messages == []


# ---------------------------------------------------------------------------
# Inline mode hard-rule block → decline injected
# ---------------------------------------------------------------------------


def test_inline_mode_hard_rule_block_injects_decline(solomon_db, monkeypatch):
    monkeypatch.delenv("SOLOMON_PIPELINE_DISABLE", raising=False)
    monkeypatch.delenv("SOLOMON_PIPELINE_MODE", raising=False)

    def mutate(event_id):
        _update_event(
            event_id,
            status="blocked_by_hard_rule",
            audit_reasoning="never discount below 12% margin",
        )

    _install_pipeline_stub(monkeypatch, mutate_event=mutate)

    c = _make_conductor()
    turn_ctx = TurnContext(raw_event=_make_raw_event("can we go to 5%"))
    c._turns["s1"] = turn_ctx

    messages: list = []
    c._pre_llm_call(session_id="s1", messages=messages)

    assert turn_ctx.status == "blocked_by_hard_rule"
    assert len(messages) == 1
    sys_msg = messages[0]
    assert sys_msg["role"] == "system"
    content = sys_msg["content"]
    assert "blocked by a hard rule" in content.lower() or "non-negotiable" in content.lower()
    assert "12% margin" in content


# ---------------------------------------------------------------------------
# Inline mode audit REJECT → decline injected
# ---------------------------------------------------------------------------


def test_inline_mode_audit_reject_injects_decline(solomon_db, monkeypatch):
    monkeypatch.delenv("SOLOMON_PIPELINE_DISABLE", raising=False)
    monkeypatch.delenv("SOLOMON_PIPELINE_MODE", raising=False)

    def mutate(event_id):
        _update_event(
            event_id,
            status="complete",
            audit_verdict="REJECT",
            audit_reasoning="proposed action contradicts foundation YAML",
            action_taken="escalate",
        )

    _install_pipeline_stub(monkeypatch, mutate_event=mutate)

    c = _make_conductor()
    turn_ctx = TurnContext(raw_event=_make_raw_event())
    c._turns["s1"] = turn_ctx

    messages: list = []
    c._pre_llm_call(session_id="s1", messages=messages)

    assert turn_ctx.status == "complete"
    assert turn_ctx.audit_verdict == "REJECT"
    assert turn_ctx.action_taken == "escalate"
    assert len(messages) == 1
    assert messages[0]["role"] == "system"
    assert "REJECTED" in messages[0]["content"]
    assert "contradicts foundation YAML" in messages[0]["content"]


# ---------------------------------------------------------------------------
# Inline mode audit REQUEST_RETHINK → rethink injected
# ---------------------------------------------------------------------------


def test_inline_mode_audit_request_rethink_injects_rethink(solomon_db, monkeypatch):
    monkeypatch.delenv("SOLOMON_PIPELINE_DISABLE", raising=False)
    monkeypatch.delenv("SOLOMON_PIPELINE_MODE", raising=False)

    def mutate(event_id):
        _update_event(
            event_id,
            status="complete",
            audit_verdict="REQUEST_RETHINK",
            audit_reasoning="surprise score 0.62 — verify the assumption first",
            action_taken="escalate",
        )

    _install_pipeline_stub(monkeypatch, mutate_event=mutate)

    c = _make_conductor()
    turn_ctx = TurnContext(raw_event=_make_raw_event())
    c._turns["s1"] = turn_ctx

    messages: list = []
    c._pre_llm_call(session_id="s1", messages=messages)

    assert turn_ctx.audit_verdict == "REQUEST_RETHINK"
    assert len(messages) == 1
    content = messages[0]["content"]
    assert "RETHINK" in content
    assert "verify the assumption" in content


# ---------------------------------------------------------------------------
# Queue mode → events row 'pending', no runner call
# ---------------------------------------------------------------------------


def test_queue_mode_inserts_pending_and_skips_runner(solomon_db, monkeypatch):
    monkeypatch.delenv("SOLOMON_PIPELINE_DISABLE", raising=False)
    monkeypatch.setenv("SOLOMON_PIPELINE_MODE", "queue")

    calls = _install_pipeline_stub(monkeypatch)

    c = _make_conductor()
    turn_ctx = TurnContext(raw_event=_make_raw_event())
    c._turns["s1"] = turn_ctx

    messages: list = []
    c._pre_llm_call(session_id="s1", messages=messages)

    # No runner invocation.
    assert calls["count"] == 0
    # Events row was inserted with status='pending'.
    assert _count_events() == 1
    assert turn_ctx.event_id is not None
    row = _read_event(turn_ctx.event_id)
    assert row["status"] == "pending"
    # No system message injected — the pipeline hasn't produced a verdict yet.
    assert messages == []
    # TurnContext is mostly empty (no row read-back in queue mode).
    assert turn_ctx.audit_verdict is None
    assert turn_ctx.classification is None


# ---------------------------------------------------------------------------
# Pipeline runner raises → caught, status='errored', legacy path runs
# ---------------------------------------------------------------------------


def test_pipeline_crash_marks_errored_and_falls_through_to_legacy(
    solomon_db, monkeypatch
):
    monkeypatch.delenv("SOLOMON_PIPELINE_DISABLE", raising=False)
    monkeypatch.delenv("SOLOMON_PIPELINE_MODE", raising=False)

    _install_pipeline_stub(monkeypatch, raise_with=RuntimeError("stage 5 boom"))

    # Legacy stubs so the fallback completes cleanly.
    class _Cls:
        def classify(self, raw_event):
            class _R:
                scope = "ops"
                domain = "general"
                decision_type = "reply"
                confidence = 0.6

            return _R()

    class _Sal:
        def score(self, raw_event, scope=None):
            class _R:
                score = 0.4
                breakdown = {}

            return _R()

    class _NN:
        def check(self, raw_event, scope=None):
            return None

    class _Hot:
        def is_thin(self):
            return True

    class _WM:
        def fetch(self, scope=None, raw_event=None):
            return _Hot()

        def update_after_turn(self, turn):
            return None

    class _Ret:
        def retrieve(self, raw_event, scope=None, domain=None):
            class _R:
                lanes = []
                heuristic_ids = []
                foundation_files = []

            return _R()

    class _S1:
        def predict(self, raw_event, scope=None, heuristics=None):
            class _R:
                answer = "ans"

            return _R()

    class _S2:
        def reason(self, raw_event, scope=None, context=None, heuristic_ids=None):
            class _R:
                answer = "deep"
                proposed_action = "reply"

            return _R()

    class _AG:
        def run(self, **kwargs):
            class _R:
                verdict = "approve"
                reasoning = "ok"

            return _R()

    class _AL:
        def level_for(self, scope):
            return "L2"

    c = _make_conductor()
    c.classifier = _Cls()
    c.salience = _Sal()
    c.non_negotiables = _NN()
    c.working_memory = _WM()
    c.retrieval = _Ret()
    c.s1 = _S1()
    c.s2 = _S2()
    c._divergence = lambda a, b: 0.0
    c.audit_gate = _AG()
    c.autonomy = _AL()

    turn_ctx = TurnContext(raw_event=_make_raw_event())
    c._turns["s1"] = turn_ctx

    messages: list = []
    c._pre_llm_call(session_id="s1", messages=messages)

    # Events row was inserted, then marked errored.
    assert _count_events() == 1
    assert turn_ctx.event_id is not None
    row = _read_event(turn_ctx.event_id)
    assert row["status"] == "errored"
    assert "pipeline error" in (row.get("audit_reasoning") or "")

    # Legacy path populated the turn (audit_verdict came from the legacy
    # audit gate stub, not the pipeline).
    assert turn_ctx.scope == "ops"
    assert turn_ctx.audit_verdict == "approve"

    # No spurious system message — legacy path doesn't inject one.
    assert messages == []


# ---------------------------------------------------------------------------
# Post-LLM mirror — idempotent, exercises the new mirror call
# ---------------------------------------------------------------------------


def test_post_llm_call_mirrors_event_to_decisions(solomon_db, monkeypatch):
    """After _pre_llm_call runs the pipeline inline, _post_llm_call should
    mirror the events row into decisions. Mirror is idempotent — if
    stage_action already mirrored, no second row is created."""
    monkeypatch.delenv("SOLOMON_PIPELINE_DISABLE", raising=False)
    monkeypatch.delenv("SOLOMON_PIPELINE_MODE", raising=False)
    monkeypatch.delenv("SOLOMON_TENANT_ID", raising=False)

    def mutate(event_id):
        _update_event(
            event_id,
            salience_score=0.6,
            classification=json.dumps(
                {"scope": "ops", "domain": "general",
                 "decision_type": "reply", "confidence": 0.7}
            ),
            system2_output=json.dumps({"proposed_action": "ack"}),
            audit_verdict="APPROVE",
            audit_reasoning="ok",
            owner_state="green",
            owner_state_ceiling=4,
            effective_autonomy=4,
            action_taken="ship",
            status="complete",
        )

    _install_pipeline_stub(monkeypatch, mutate_event=mutate)

    c = _make_conductor()

    # Stub the DecisionLog + downstream side effects so _post_llm_call
    # doesn't need the full Conductor wiring.
    class _DL:
        def log(self, turn):
            return 999

    class _PS:
        def store_for_decision(self, did, turn):
            return None

    class _WM:
        def update_after_turn(self, turn):
            return None

    c.decision_log = _DL()
    c.predictions = _PS()
    c.counterfactuals = _PS()
    c.working_memory = _WM()

    turn_ctx = TurnContext(raw_event=_make_raw_event())
    c._turns["s1"] = turn_ctx

    c._pre_llm_call(session_id="s1", messages=[])
    event_id = turn_ctx.event_id
    assert event_id is not None

    # stage_action mirror is bypassed (we stubbed the runner), so the
    # decisions table is empty at this point.
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT COUNT(*) FROM decisions WHERE event_id = ?", (event_id,))
            assert cur.fetchone()[0] == 0

    # Post-LLM hook should create the decisions row.
    c._post_llm_call(session_id="s1", response="ack")

    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT COUNT(*) FROM decisions WHERE event_id = ?", (event_id,))
            assert cur.fetchone()[0] == 1

    # Idempotency: calling mirror again must NOT create a second row.
    dec.mirror_event_to_decision(event_id)
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT COUNT(*) FROM decisions WHERE event_id = ?", (event_id,))
            assert cur.fetchone()[0] == 1

"""Tests for slash command handlers — real Hermes signature."""

from __future__ import annotations

from pathlib import Path

from solomon import profile, session_state, slash


# ---------------------------------------------------------------------------
# /onboard
# ---------------------------------------------------------------------------


def test_onboard_returns_text_and_pushes_intent(solomon_home: Path):
    profile.init_solomon_home()
    out = slash.cmd_onboard("")
    assert isinstance(out, str)
    assert "session 0" in out
    assert "Industry & sector" in out
    # The next pre_llm_call should be able to claim this intent.
    intent = session_state.claim_pending_intent("sess-x")
    assert intent is not None
    assert intent["intent"] == "onboarding"
    assert intent["session_n"] == 0


def test_onboard_advances_after_session_0_filled(solomon_home: Path):
    profile.init_solomon_home()
    profile.write_session_summary(0, {
        "business_category": "x", "primary_product_or_service": "x",
        "customer_orientation": "B2B", "geographic_scope": "local",
        "revenue_model": "project", "growth_stage": "early",
        "concentration_risk": "low",
    })
    out = slash.cmd_onboard("")
    assert "session 1" in out
    intent = session_state.claim_pending_intent("s")
    assert intent["session_n"] == 1


def test_onboard_all_done(solomon_home: Path):
    profile.init_solomon_home()
    # Fill all 7 sessions.
    for n, fields in profile.SESSION_REQUIRED_FIELDS.items():
        summary = {}
        for f in fields:
            if f in ("core_beliefs", "what_they_reject", "not_for",
                      "decision_principles", "trade_off_principles"):
                summary[f] = ["x"]
            elif f == "rules":
                summary[f] = [{"rule": "x", "why": "y"}]
            elif f == "list":
                summary[f] = [{"name": "x", "autonomy": "watch"}]
            else:
                summary[f] = "x"
        profile.write_session_summary(n, summary)
    out = slash.cmd_onboard("")
    assert "complete" in out


# ---------------------------------------------------------------------------
# /mentor
# ---------------------------------------------------------------------------


def test_mentor_empty_queue(solomon_home: Path):
    profile.init_solomon_home()
    out = slash.cmd_mentor("")
    assert "Nothing in your queue" in out
    # Still pushes the intent so the next turn enters mentoring mode.
    intent = session_state.claim_pending_intent("s")
    assert intent["intent"] == "mentoring"


def test_mentor_with_pending_items(solomon_home: Path):
    profile.init_solomon_home()
    profile.append_review_item({"kind": "addition", "file": "finance",
                                  "section": "x", "content": "y", "reason": "z"})
    out = slash.cmd_mentor("")
    assert "review item" in out


def test_mentor_with_ignored_actions(solomon_home: Path):
    profile.init_solomon_home()
    profile.append_action_item({
        "source_kind": "email", "source_id": "<m1>",
        "source_summary": "x", "urgency": "low",
        "action_kind": "draft_reply", "nudge_count": 3,
    })
    out = slash.cmd_mentor("")
    assert "ignoring" in out


# ---------------------------------------------------------------------------
# /status — no pending intent, just text
# ---------------------------------------------------------------------------


def test_status_first_run(solomon_home: Path):
    profile.init_solomon_home()
    out = slash.cmd_status("")
    assert "0 of 7 complete" in out
    assert "/onboard" in out


def test_status_after_session_0(solomon_home: Path):
    profile.init_solomon_home()
    profile.write_session_summary(0, {
        "business_category": "x", "primary_product_or_service": "x",
        "customer_orientation": "B2B", "geographic_scope": "local",
        "revenue_model": "project", "growth_stage": "early",
        "concentration_risk": "low",
    })
    out = slash.cmd_status("")
    assert "1 of 7 complete" in out


def test_status_shows_pending_actions(solomon_home: Path):
    profile.init_solomon_home()
    profile.append_action_item({
        "source_kind": "email", "source_id": "<m1>",
        "source_summary": "vendor question", "urgency": "high",
        "action_kind": "draft_reply",
    })
    out = slash.cmd_status("")
    assert "Pending actions: 1" in out
    assert "1 high" in out


# ---------------------------------------------------------------------------
# /private + /endprivate
# ---------------------------------------------------------------------------


def test_private_pushes_intent(solomon_home: Path):
    profile.init_solomon_home()
    out = slash.cmd_private("")
    assert "Private mode" in out
    intent = session_state.claim_pending_intent("s")
    assert intent["intent"] == "private_on"


def test_endprivate_pushes_intent(solomon_home: Path):
    profile.init_solomon_home()
    out = slash.cmd_endprivate("")
    assert "Private mode" in out
    intent = session_state.claim_pending_intent("s")
    assert intent["intent"] == "private_off"


# ---------------------------------------------------------------------------
# /solomon-off + /solomon-on
# ---------------------------------------------------------------------------


def test_solomon_off_creates_sentinel(solomon_home: Path):
    profile.init_solomon_home()
    out = slash.cmd_solomon_off("")
    assert (solomon_home / ".solomon_off").exists()
    assert "suspended" in out


def test_solomon_on_removes_sentinel(solomon_home: Path):
    profile.init_solomon_home()
    slash.cmd_solomon_off("")
    out = slash.cmd_solomon_on("")
    assert not (solomon_home / ".solomon_off").exists()
    assert "active again" in out


# ---------------------------------------------------------------------------
# /ingest empty inbox
# ---------------------------------------------------------------------------


def test_ingest_empty_inbox(solomon_home: Path):
    profile.init_solomon_home()
    out = slash.cmd_ingest("")
    assert "Inbox is empty" in out


# ---------------------------------------------------------------------------
# Registration + error wrapping
# ---------------------------------------------------------------------------


def test_register_all_uses_hermes_signature(solomon_home: Path):
    calls = []

    class FakeAdapter:
        def register_command(self, *, name, description, handler):
            calls.append((name, handler))

    slash.register_all(FakeAdapter())
    names = {c[0] for c in calls}
    assert names == {"onboard", "mentor", "status", "private", "endprivate",
                      "reflect", "ingest", "solomon-off", "solomon-on"}
    # Every handler accepts a raw string and returns a string (or None).
    for _, h in calls:
        result = h("")
        assert result is None or isinstance(result, str)


def test_wrapped_handler_swallows_exceptions(solomon_home: Path):
    """If an inner handler raises, the wrapper returns a user-facing
    string and logs the error. Exception does not escape to Hermes."""
    def boom(raw):
        raise RuntimeError("kaboom")

    wrapped = slash._wrap_handler(boom)
    out = wrapped("")
    assert isinstance(out, str)
    assert "error" in out.lower()

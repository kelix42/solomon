"""Tests for slash command handlers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from solomon import profile, slash


def test_onboard_first_run_returns_session_0(solomon_home: Path):
    session = SimpleNamespace(private=False)
    result = slash.cmd_onboard({}, session)
    assert result["ok"]
    assert result["session_n"] == 0
    assert "session 0" in result["message"]
    assert session.solomon_skill_overridden is True
    assert "MODE: onboarding" in result["system_prompt"]
    assert "business_category" in result["system_prompt"]


def test_onboard_skips_to_next_unfilled(solomon_home: Path):
    profile.init_solomon_home()
    profile.write_session_summary(0, {
        "business_category": "x", "primary_product_or_service": "x",
        "customer_orientation": "B2B", "geographic_scope": "local",
        "revenue_model": "project", "growth_stage": "early",
        "concentration_risk": "low",
    })
    session = SimpleNamespace(private=False)
    result = slash.cmd_onboard({}, session)
    assert result["session_n"] == 1


def test_onboard_all_done_message(solomon_home: Path):
    profile.init_solomon_home()
    # Fill all 7 sessions.
    for n, fields in profile.SESSION_REQUIRED_FIELDS.items():
        summary = {f: ("x" if isinstance(f, str) else "") for f in fields}
        # Fill list-typed fields with actual lists.
        for f in ("core_beliefs", "what_they_reject", "not_for",
                  "decision_principles", "trade_off_principles", "rules", "list"):
            if f in summary:
                summary[f] = ["x"] if f != "list" else [{"name": "x", "autonomy": "watch"}]
        if "preferred_channel" in fields:
            summary["preferred_channel"] = "telegram"
        profile.write_session_summary(n, summary)
    result = slash.cmd_onboard({}, SimpleNamespace(private=False))
    assert "complete" in result["message"]
    assert result["system_prompt"] is None


def test_status_first_run(solomon_home: Path):
    result = slash.cmd_status({}, None)
    assert result["ok"]
    assert "0 of 7 complete" in result["message"]
    assert "/onboard" in result["message"]


def test_status_after_session_0(solomon_home: Path):
    profile.init_solomon_home()
    profile.write_session_summary(0, {
        "business_category": "x", "primary_product_or_service": "x",
        "customer_orientation": "B2B", "geographic_scope": "local",
        "revenue_model": "project", "growth_stage": "early",
        "concentration_risk": "low",
    })
    result = slash.cmd_status({}, None)
    assert "1 of 7 complete" in result["message"]


def test_status_shows_pending_actions(solomon_home: Path):
    profile.init_solomon_home()
    profile.append_action_item({
        "source_kind": "email", "source_id": "<m1>",
        "source_summary": "vendor question", "urgency": "high",
        "action_kind": "draft_reply",
    })
    result = slash.cmd_status({}, None)
    assert "Pending actions: 1" in result["message"]
    assert "1 high" in result["message"]


def test_private_toggle(solomon_home: Path):
    session = SimpleNamespace(private=False, id="s1")
    r1 = slash.cmd_private({}, session)
    assert session.private is True
    assert "is on" in r1["message"]
    r2 = slash.cmd_private({}, session)
    assert session.private is False
    assert "is off" in r2["message"]


def test_solomon_off_creates_sentinel(solomon_home: Path):
    slash.cmd_solomon_off({}, None)
    assert (solomon_home / ".solomon_off").exists()


def test_solomon_on_removes_sentinel(solomon_home: Path):
    slash.cmd_solomon_off({}, None)
    slash.cmd_solomon_on({}, None)
    assert not (solomon_home / ".solomon_off").exists()


def test_mentor_with_empty_queue(solomon_home: Path):
    profile.init_solomon_home()
    session = SimpleNamespace(private=False)
    result = slash.cmd_mentor({}, session)
    assert result["ok"]
    assert "Nothing in your queue" in result["message"]
    assert session.solomon_skill_overridden is True


def test_mentor_with_items(solomon_home: Path):
    profile.init_solomon_home()
    profile.append_review_item({"kind": "addition", "file": "finance",
                                  "section": "Test", "content": "x",
                                  "reason": "test"})
    profile.append_action_item({
        "source_kind": "email", "source_id": "<m1>",
        "source_summary": "x", "urgency": "low",
        "action_kind": "draft_reply", "nudge_count": 3,
    })
    result = slash.cmd_mentor({}, SimpleNamespace(private=False))
    assert "review item" in result["message"]
    assert "ignoring" in result["message"]


def test_ingest_empty_inbox(solomon_home: Path):
    profile.init_solomon_home()
    result = slash.cmd_ingest({}, None)
    assert "Inbox is empty" in result["message"]


def test_register_all_calls_adapter(solomon_home: Path):
    calls = []

    class FakeAdapter:
        def register_command(self, *, name, description, handler):
            calls.append((name, handler))

    slash.register_all(FakeAdapter())
    names = [c[0] for c in calls]
    assert set(names) == {
        "onboard", "mentor", "status", "private",
        "reflect", "ingest", "solomon-off", "solomon-on",
    }


def test_handler_swallows_exceptions(solomon_home: Path):
    """Handlers wrap their inner function in try/except so errors don't crash Hermes."""
    calls = []

    class FakeAdapter:
        def register_command(self, *, name, description, handler):
            calls.append(handler)

    slash.register_all(FakeAdapter())
    # The status handler should never raise.
    for h in calls:
        result = h({})
        # All handlers either return ok=True or ok=False with a message.
        assert "ok" in result

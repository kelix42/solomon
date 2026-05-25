"""Tests for the proactive inbound flow — post-turn notification +
owner-reply parsing + action dispatching."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from solomon import inbound, profile, tools


class FakeAdapter:
    """send_to_owner records calls; read_conversations etc. unused here."""

    def __init__(self, succeed=True):
        self.sent: list[tuple[str, Any]] = []
        self.succeed = succeed

    def send_to_owner(self, text, target=None):
        self.sent.append((text, target))
        return self.succeed

    def read_conversations(self, *, since, limit, exclude_session_ids):
        return []


def _set_adapter(monkeypatch, adapter):
    """Install the adapter at the tools module level (mirrors what
    register_all does at plugin start)."""
    monkeypatch.setattr(tools, "_adapter", adapter)


# ---------------------------------------------------------------------------
# dispatch_pending_notifications
# ---------------------------------------------------------------------------


def test_dispatch_sends_for_new_pending_actions(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    a = FakeAdapter()
    _set_adapter(monkeypatch, a)
    iid = tools.propose_action(
        source_kind="email", source_id="<m1>", source_summary="vendor",
        first_pass_prediction="ack", final_recommendation="reply soon",
        reasoning="vendor policy", urgency="medium", action_kind="draft_reply",
    )
    sent = inbound.dispatch_pending_notifications()
    assert sent == 1
    assert a.sent and "vendor" in a.sent[0][0]
    # The item is now marked notified.
    item = profile.find_queue_item("actions", iid)
    assert item["owner_notified_at"]


def test_dispatch_skips_already_notified(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    a = FakeAdapter()
    _set_adapter(monkeypatch, a)
    tools.propose_action(
        source_kind="email", source_id="<m2>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="low", action_kind="record_only",
    )
    inbound.dispatch_pending_notifications()
    a.sent.clear()
    inbound.dispatch_pending_notifications()
    assert a.sent == []


def test_dispatch_queues_on_send_failure(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    a = FakeAdapter(succeed=False)
    _set_adapter(monkeypatch, a)
    tools.propose_action(
        source_kind="email", source_id="<m3>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="low", action_kind="record_only",
    )
    inbound.dispatch_pending_notifications()
    pending_file = solomon_home / "pending_messages.jsonl"
    assert pending_file.exists()


def test_dispatch_no_adapter_returns_zero(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    _set_adapter(monkeypatch, None)
    tools.propose_action(
        source_kind="email", source_id="<m4>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="low", action_kind="record_only",
    )
    assert inbound.dispatch_pending_notifications() == 0


# ---------------------------------------------------------------------------
# parse_owner_decision
# ---------------------------------------------------------------------------


def test_parse_decision_approve_with_implicit_id(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    _set_adapter(monkeypatch, FakeAdapter())
    iid = tools.propose_action(
        source_kind="email", source_id="<m5>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="low", action_kind="record_only",
    )
    inbound.dispatch_pending_notifications()
    parsed = inbound.parse_owner_decision("approve")
    assert parsed == (iid, "approve", None)


def test_parse_decision_explicit_id(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    _set_adapter(monkeypatch, FakeAdapter())
    iid = tools.propose_action(
        source_kind="email", source_id="<m6>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="low", action_kind="record_only",
    )
    inbound.dispatch_pending_notifications()
    parsed = inbound.parse_owner_decision(f"reject {iid}")
    assert parsed == (iid, "reject", None)


def test_parse_decision_edit_with_body(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    _set_adapter(monkeypatch, FakeAdapter())
    iid = tools.propose_action(
        source_kind="email", source_id="<m7>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="low", action_kind="draft_reply",
        action_payload={"to": "x@example.com", "body": "draft"},
    )
    inbound.dispatch_pending_notifications()
    parsed = inbound.parse_owner_decision(
        "edit: send them my best regards instead",
    )
    assert parsed is not None
    pid, dec, edits = parsed
    assert pid == iid and dec == "edit"
    assert "best regards" in edits


def test_parse_decision_no_match(solomon_home: Path):
    assert inbound.parse_owner_decision("hello, how are you") is None


def test_parse_decision_no_pending(solomon_home: Path):
    profile.init_solomon_home()
    # No notified pending action exists.
    assert inbound.parse_owner_decision("approve") is None


# ---------------------------------------------------------------------------
# apply_owner_decision and dispatch_action
# ---------------------------------------------------------------------------


def test_apply_owner_decision_record_only(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    a = FakeAdapter()
    _set_adapter(monkeypatch, a)
    iid = tools.propose_action(
        source_kind="email", source_id="<m8>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="low", action_kind="record_only",
    )
    inbound.dispatch_pending_notifications()
    inbound.apply_owner_decision(iid, "approve")
    item = profile.find_queue_item("actions", iid)
    assert item["status"] == "dispatched"


def test_apply_owner_decision_reject(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    _set_adapter(monkeypatch, FakeAdapter())
    iid = tools.propose_action(
        source_kind="email", source_id="<m9>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="low", action_kind="draft_reply",
    )
    inbound.dispatch_pending_notifications()
    inbound.apply_owner_decision(iid, "reject")
    item = profile.find_queue_item("actions", iid)
    assert item["status"] == "rejected"


def test_dispatch_action_draft_reply_sends_via_adapter(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    a = FakeAdapter()
    _set_adapter(monkeypatch, a)
    iid = tools.propose_action(
        source_kind="email", source_id="vendor-1", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="medium", action_kind="draft_reply",
        action_payload={"to": "vendor@example.com",
                          "subject": "Re: invoice",
                          "body": "Hi — quick question."},
    )
    inbound.dispatch_pending_notifications()
    # Note: dispatch sent the notification. Now approve.
    a.sent.clear()
    inbound.apply_owner_decision(iid, "approve")
    # The drafted reply was sent (the second send_to_owner call).
    assert a.sent  # at least one send happened
    assert "quick question" in a.sent[0][0]
    item = profile.find_queue_item("actions", iid)
    assert item["status"] == "dispatched"


def test_dispatch_action_uses_owner_edits(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    a = FakeAdapter()
    _set_adapter(monkeypatch, a)
    iid = tools.propose_action(
        source_kind="email", source_id="vendor-2", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="medium", action_kind="draft_reply",
        action_payload={"to": "v@x", "body": "original draft"},
    )
    inbound.dispatch_pending_notifications()
    a.sent.clear()
    inbound.apply_owner_decision(iid, "edit",
                                   edited_content="totally rewritten body")
    # The drafted reply uses the owner's edit, not the original.
    assert any("totally rewritten body" in s[0] for s in a.sent)


def test_dispatch_action_schedule_event_logs_pending_integration(solomon_home: Path,
                                                                   monkeypatch):
    profile.init_solomon_home()
    _set_adapter(monkeypatch, FakeAdapter())
    iid = tools.propose_action(
        source_kind="email", source_id="<m10>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="low", action_kind="schedule_event",
    )
    inbound.dispatch_pending_notifications()
    inbound.apply_owner_decision(iid, "approve")
    item = profile.find_queue_item("actions", iid)
    # Still marked dispatched even though no real calendar tool yet.
    assert item["status"] == "dispatched"
    # And a warning landed in the log.
    from solomon import logs as logsmod
    assert "action_handler_pending_integration" in logsmod.log_path().read_text()


def test_dispatch_action_unknown_kind_marks_failed(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    _set_adapter(monkeypatch, FakeAdapter())
    # Bypass propose_action's enum validation by appending directly.
    iid = profile.append_action_item({
        "source_kind": "email", "source_id": "<m11>",
        "source_summary": "x", "urgency": "low",
        "action_kind": "weird_kind", "status": "approved",
        "owner_decision": "approve",
    })
    item = profile.find_queue_item("actions", iid)
    inbound.dispatch_action(item)
    final = profile.find_queue_item("actions", iid)
    assert final["status"] == "dispatch_failed"


def test_dispatch_action_skips_non_approved(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    _set_adapter(monkeypatch, FakeAdapter())
    iid = profile.append_action_item({
        "source_kind": "email", "source_id": "<m12>",
        "source_summary": "x", "urgency": "low",
        "action_kind": "draft_reply", "status": "pending",
    })
    item = profile.find_queue_item("actions", iid)
    # Not yet approved — dispatch should skip.
    assert inbound.dispatch_action(item) is False

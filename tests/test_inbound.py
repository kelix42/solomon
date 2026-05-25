"""Tests for the proactive inbound flow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

from solomon import inbound, profile, tools


class FakeAdapter:
    """Captures send_to_owner calls. Configurable success/failure."""

    def __init__(self, succeed=True):
        self.succeed = succeed
        self.sent: list[tuple[str, str]] = []
        self.ctx = SimpleNamespace()

    def send_to_owner(self, text, *, channel=None):
        self.sent.append((text, channel or ""))
        return self.succeed

    def llm_call(self, *, system, messages, json_mode=False, max_tokens=2048):
        return "Quick nudge: still need your call on this."


def _set_preferred(ch: str, home: Path):
    data = yaml.safe_load((home / "profile.yaml").read_text())
    data.setdefault("meta", {})["preferred_channel"] = ch
    (home / "profile.yaml").write_text(yaml.safe_dump(data, sort_keys=False))


def test_dispatch_pending_notifications_sends(solomon_home: Path):
    profile.init_solomon_home()
    _set_preferred("telegram", solomon_home)
    iid = tools.propose_action(
        source_kind="email", source_id="<m1>", source_summary="vendor",
        first_pass_prediction="ack", final_recommendation="reply requesting call",
        reasoning="vendor policy", urgency="medium", action_kind="draft_reply",
    )
    a = FakeAdapter()
    sent = inbound.dispatch_pending_notifications(adapter=a)
    assert sent == 1
    assert a.sent and "vendor" in a.sent[0][0]
    item = profile.find_queue_item("actions", iid)
    assert item["owner_notified_at"]
    assert item["owner_notified_via"] == "telegram"


def test_dispatch_pending_falls_back_to_queue_on_failure(solomon_home: Path):
    profile.init_solomon_home()
    _set_preferred("telegram", solomon_home)
    tools.propose_action(
        source_kind="email", source_id="<m2>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="low", action_kind="record_only",
    )
    a = FakeAdapter(succeed=False)
    inbound.dispatch_pending_notifications(adapter=a)
    assert (solomon_home / "pending_messages.jsonl").exists()


def test_dispatch_pending_skips_already_notified(solomon_home: Path):
    profile.init_solomon_home()
    iid = tools.propose_action(
        source_kind="email", source_id="<m3>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="low", action_kind="record_only",
    )
    a = FakeAdapter()
    inbound.dispatch_pending_notifications(adapter=a)
    a.sent.clear()
    inbound.dispatch_pending_notifications(adapter=a)
    assert not a.sent


def test_parse_owner_decision_approve(solomon_home: Path):
    profile.init_solomon_home()
    iid = tools.propose_action(
        source_kind="email", source_id="<m4>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="low", action_kind="record_only",
    )
    inbound.dispatch_pending_notifications(adapter=FakeAdapter())
    parsed = inbound.parse_owner_decision("approve")
    assert parsed == (iid, "approve", None)


def test_parse_owner_decision_with_explicit_id(solomon_home: Path):
    profile.init_solomon_home()
    tools.propose_action(
        source_kind="email", source_id="<m5>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="low", action_kind="record_only",
    )
    inbound.dispatch_pending_notifications(adapter=FakeAdapter())
    items = profile.read_queue("actions", status="pending")
    iid = items[0]["id"]
    parsed = inbound.parse_owner_decision(f"approve {iid}")
    assert parsed == (iid, "approve", None)


def test_parse_owner_decision_edit_with_body(solomon_home: Path):
    profile.init_solomon_home()
    iid = tools.propose_action(
        source_kind="email", source_id="<m6>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="low", action_kind="draft_reply",
        action_payload={"to": "x@example.com", "body": "draft"},
    )
    inbound.dispatch_pending_notifications(adapter=FakeAdapter())
    parsed = inbound.parse_owner_decision("edit: send them my best regards instead")
    assert parsed is not None
    pid, dec, edits = parsed
    assert pid == iid
    assert dec == "edit"
    assert "best regards" in edits


def test_parse_owner_decision_non_decision_text(solomon_home: Path):
    assert inbound.parse_owner_decision("hello, how are you") is None


def test_apply_owner_decision_dispatches(solomon_home: Path):
    profile.init_solomon_home()
    iid = tools.propose_action(
        source_kind="email", source_id="<m7>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="low", action_kind="record_only",
    )
    inbound.dispatch_pending_notifications(adapter=FakeAdapter())
    inbound.apply_owner_decision(iid, "approve", None)
    item = profile.find_queue_item("actions", iid)
    assert item["status"] == "dispatched"


def test_nudge_step_sends_when_due(solomon_home: Path):
    profile.init_solomon_home()
    _set_preferred("telegram", solomon_home)
    tools.propose_action(
        source_kind="email", source_id="<m8>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="high", action_kind="record_only",
    )
    # Notify with an old timestamp so a nudge is due now.
    items = profile.read_queue("actions", status="pending")
    iid = items[0]["id"]
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    profile.update_queue_item("actions", iid, {
        "owner_notified_at": long_ago, "owner_notified_via": "telegram",
    })
    a = FakeAdapter()
    result = inbound.nudge_step(adapter=a)
    assert result["sent"] == 1
    assert a.sent  # nudge dispatched


def test_nudge_step_no_op_when_not_due(solomon_home: Path):
    profile.init_solomon_home()
    tools.propose_action(
        source_kind="email", source_id="<m9>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="low", action_kind="record_only",
    )
    inbound.dispatch_pending_notifications(adapter=FakeAdapter())
    a = FakeAdapter()
    result = inbound.nudge_step(adapter=a)
    assert result["sent"] == 0


def test_retry_pending_messages(solomon_home: Path):
    profile.init_solomon_home()
    inbound._queue_pending_message("queued msg", "telegram")
    a = FakeAdapter(succeed=True)
    sent = inbound.retry_pending_messages(adapter=a)
    assert sent == 1
    # File removed since everything sent.
    assert not (solomon_home / "pending_messages.jsonl").exists()

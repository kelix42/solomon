"""Tests for the daily cron."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from solomon import daily, profile, tools


class FakeAdapter:
    def __init__(self, convos=None, send_ok=True):
        self.convos = convos or []
        self.send_ok = send_ok
        self.sent: list = []
        self.ctx = SimpleNamespace()

    def read_recent_conversations(self, since):
        return self.convos

    def send_to_owner(self, text, *, channel=None):
        self.sent.append((text, channel))
        return self.send_ok

    def llm_call(self, **kwargs):
        return "ok"


def test_daily_run_no_adapter_returns_zeroes(solomon_home: Path):
    profile.init_solomon_home()
    summary = daily.run(adapter=None)
    assert summary["batches"] == 0
    assert summary["files"] == 0


def test_reflect_step_skips_private_conversations(solomon_home: Path):
    profile.init_solomon_home()
    adapter = FakeAdapter(convos=[
        {"session_id": "s1", "private": True, "turns": [
            {"role": "user", "content": "secret thing"}]},
    ])
    batches = daily.reflect_step(adapter=adapter)
    assert batches == 0


def test_reflect_step_processes_substantive_conversations(solomon_home: Path):
    profile.init_solomon_home()
    convo = {
        "session_id": "s2",
        "private": False,
        "turns": [
            {"role": "user", "content": "We just signed up a new vendor today, they handle our court filings."},
            {"role": "assistant", "content": "Noted. Who is it?"},
        ],
    }
    adapter = FakeAdapter(convos=[convo])
    batches = daily.reflect_step(adapter=adapter)
    assert batches == 1
    assert adapter.llm_call.__name__ == "llm_call"  # sanity


def test_daily_run_processes_inbox(solomon_home: Path):
    profile.init_solomon_home()
    (solomon_home / "inbox" / "doc.txt").write_text("policy: discounts cap at 15%")
    adapter = FakeAdapter()
    summary = daily.run(adapter=adapter)
    assert summary["files"] == 1


def test_daily_run_nudges_overdue_actions(solomon_home: Path):
    profile.init_solomon_home()
    iid = tools.propose_action(
        source_kind="email", source_id="<dnudge>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="high", action_kind="record_only",
    )
    # Stamp notification 5h ago so a nudge is due.
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    profile.update_queue_item("actions", iid, {
        "owner_notified_at": long_ago, "owner_notified_via": "telegram",
    })
    adapter = FakeAdapter()
    summary = daily.run(adapter=adapter)
    assert summary["nudges_sent"] >= 1


def test_daily_run_acquires_lock_skips_if_held(solomon_home: Path):
    profile.init_solomon_home()
    # Open the lock first ourselves to block the cron.
    import fcntl
    f = open(daily._lock_path(), "w")
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    try:
        summary = daily.run(adapter=None)
        assert summary.get("skipped") is True
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def test_daily_run_continues_on_step_failure(solomon_home: Path):
    profile.init_solomon_home()

    class BadAdapter:
        ctx = SimpleNamespace()

        def read_recent_conversations(self, since):
            raise RuntimeError("hermes is sick")

        def send_to_owner(self, text, *, channel=None):
            return True

        def llm_call(self, **kwargs):
            return "ok"

    # Should not raise.
    summary = daily.run(adapter=BadAdapter())
    assert "batches" in summary

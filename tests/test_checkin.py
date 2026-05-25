"""Tests for the weekly check-in cron."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

from solomon import checkin, profile


class FakeLLM:
    def __init__(self, response="Hey — quick check-in: your marketing is thin. Want to talk?", send_ok=True):
        self.response = response
        self.send_ok = send_ok
        self.sent: list = []
        self.ctx = SimpleNamespace()

    def llm_call(self, *, system, messages, **kwargs):
        return self.response

    def read_recent_conversations(self, since):
        return [{"session_id": "s1", "turns": [], "private": False}]

    def send_to_owner(self, text, *, channel=None):
        self.sent.append((text, channel))
        return self.send_ok


def _set_channel(home: Path, ch: str):
    data = yaml.safe_load((home / "profile.yaml").read_text())
    data.setdefault("meta", {})["preferred_channel"] = ch
    (home / "profile.yaml").write_text(yaml.safe_dump(data, sort_keys=False))


def test_checkin_no_adapter_returns_empty(solomon_home: Path):
    profile.init_solomon_home()
    summary = checkin.run(adapter=None)
    assert summary["sent"] is False


def test_checkin_sends_via_preferred_channel(solomon_home: Path):
    profile.init_solomon_home()
    _set_channel(solomon_home, "telegram")
    adapter = FakeLLM()
    summary = checkin.run(adapter=adapter)
    assert summary["sent"] is True
    assert adapter.sent and adapter.sent[0][1] == "telegram"


def test_checkin_queues_on_send_failure(solomon_home: Path):
    profile.init_solomon_home()
    _set_channel(solomon_home, "telegram")
    adapter = FakeLLM(send_ok=False)
    summary = checkin.run(adapter=adapter)
    assert summary["queued"] is True
    assert (solomon_home / "pending_messages.jsonl").exists()


def test_checkin_handles_llm_failure(solomon_home: Path):
    profile.init_solomon_home()

    class BadLLM:
        def llm_call(self, **kwargs):
            raise RuntimeError("LLM down")

        def read_recent_conversations(self, since):
            return []

        def send_to_owner(self, text, *, channel=None):
            return True

    summary = checkin.run(adapter=BadLLM())
    assert summary["sent"] is False


def test_checkin_handles_empty_llm_response(solomon_home: Path):
    profile.init_solomon_home()
    adapter = FakeLLM(response="")
    summary = checkin.run(adapter=adapter)
    assert summary["sent"] is False


def test_checkin_lock_skip(solomon_home: Path):
    profile.init_solomon_home()
    import fcntl
    f = open(checkin._lock_path(), "w")
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    try:
        summary = checkin.run(adapter=None)
        assert summary.get("lock_skipped") is True
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()

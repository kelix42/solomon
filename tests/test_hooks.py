"""Tests for hooks.py — system prompt injection and bypass paths."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from solomon import hooks, profile


def test_pre_llm_call_injects_system_block(solomon_home: Path):
    profile.init_solomon_home()
    messages = [{"role": "user", "content": "hello"}]
    session = SimpleNamespace(id="s1", private=False)
    hooks.pre_llm_call(messages, session)
    assert messages[0]["role"] == "system"
    assert "Solomon role" in messages[0]["content"]
    assert "Owner vocabulary" in messages[0]["content"]
    assert "Profile summary" in messages[0]["content"]
    assert "Available tools:" in messages[0]["content"]


def test_pre_llm_call_skipped_when_solomon_off(solomon_home: Path):
    profile.init_solomon_home()
    (solomon_home / ".solomon_off").touch()
    messages = [{"role": "user", "content": "hi"}]
    session = SimpleNamespace(id="s1", private=False)
    hooks.pre_llm_call(messages, session)
    assert all(m["role"] != "system" or "Solomon role" not in m["content"]
               for m in messages)


def test_pre_llm_call_skipped_when_private(solomon_home: Path):
    profile.init_solomon_home()
    messages = [{"role": "user", "content": "hi"}]
    session = SimpleNamespace(id="s1", private=True)
    hooks.pre_llm_call(messages, session)
    assert len(messages) == 1  # nothing prepended


def test_pre_llm_call_consumes_override_flag(solomon_home: Path):
    profile.init_solomon_home()
    messages = [{"role": "user", "content": "hi"}]
    session = SimpleNamespace(id="s1", private=False,
                                solomon_skill_overridden=True)
    hooks.pre_llm_call(messages, session)
    assert len(messages) == 1
    assert session.solomon_skill_overridden is False


def test_pre_llm_call_empty_profile_invites_onboard(solomon_home: Path):
    profile.init_solomon_home()
    messages = [{"role": "user", "content": "hi"}]
    session = SimpleNamespace(id="s1", private=False)
    hooks.pre_llm_call(messages, session)
    assert "/onboard" in messages[0]["content"]


def test_pre_llm_call_detects_external_inbound(solomon_home: Path):
    profile.init_solomon_home()
    messages = [{
        "role": "user",
        "content": "from a customer",
        "source": {"kind": "email", "id": "<m-1>", "channel": "email"},
    }]
    session = SimpleNamespace(id="s1", private=False)
    hooks.pre_llm_call(messages, session)
    assert "INBOUND CONTEXT" in messages[0]["content"]
    assert "email" in messages[0]["content"]


def test_pre_llm_call_does_not_flag_owner_direct_message(solomon_home: Path):
    profile.init_solomon_home()
    messages = [{"role": "user", "content": "hello",
                  "source": {"kind": "cli"}}]
    session = SimpleNamespace(id="s1", private=False)
    hooks.pre_llm_call(messages, session)
    assert "INBOUND CONTEXT" not in messages[0]["content"]


def test_on_session_start_initializes_private_false(solomon_home: Path):
    session = SimpleNamespace(id="s1")
    hooks.on_session_start(session)
    assert session.private is False


def test_post_llm_call_logs_private_turn_when_private(solomon_home: Path):
    profile.init_solomon_home()
    session = SimpleNamespace(id="s1", private=True)
    response = SimpleNamespace(input_tokens=10, output_tokens=20)
    hooks.post_llm_call(response, session)
    # Reading the log: should have a private_turn entry.
    from solomon import logs as logsmod
    text = logsmod.log_path().read_text()
    assert "private_turn" in text

"""Tests for the Hermes lifecycle hooks — real signatures + context-return."""

from __future__ import annotations

import json
from pathlib import Path

from solomon import hooks, profile, session_state


# ---------------------------------------------------------------------------
# Default-mode context injection
# ---------------------------------------------------------------------------


def test_pre_llm_call_returns_context_dict(solomon_home: Path):
    profile.init_solomon_home()
    result = hooks.pre_llm_call(
        session_id="sess-1",
        user_message="hello",
        conversation_history=[],
        is_first_turn=True,
        model="claude-sonnet-4-6",
        platform="cli",
    )
    assert isinstance(result, dict)
    assert "context" in result
    block = result["context"]
    assert "Solomon role" in block
    assert "Available tools" in block


def test_pre_llm_call_includes_vocabulary_and_summary(solomon_home: Path):
    profile.init_solomon_home()
    # Seed the vocabulary file with a phrase.
    (solomon_home / "vocabulary.md").write_text(
        "# Vocabulary\n\n## Phrases\n\n- 'the build sheet'\n", encoding="utf-8"
    )
    block = hooks.pre_llm_call(session_id="s", user_message="x",
                                  conversation_history=[], is_first_turn=True,
                                  model="m", platform="cli")["context"]
    assert "the build sheet" in block


def test_pre_llm_call_empty_profile_invites_onboard(solomon_home: Path):
    profile.init_solomon_home()
    block = hooks.pre_llm_call(session_id="s", user_message="hi",
                                  conversation_history=[], is_first_turn=True,
                                  model="m", platform="cli")["context"]
    assert "/onboard" in block


def test_pre_llm_call_partial_profile_shows_progress_not_empty(solomon_home: Path):
    """Sessions completed but the weekly summary not yet regenerated must NOT
    read as an empty profile. Regression for the 'profile is empty' lie."""
    profile.init_solomon_home()
    profile.write_session_summary(5, {"rules": ["Never push a closing that isn't ready."]})
    block = hooks.pre_llm_call(session_id="s", user_message="hi",
                                  conversation_history=[], is_first_turn=True,
                                  model="m", platform="cli")["context"]
    assert "1 of 7" in block
    assert "Non-negotiables" in block
    # The old lie must be gone.
    assert "profile.yaml is empty" not in block
    assert "no profile yet" not in block


# ---------------------------------------------------------------------------
# Bypasses
# ---------------------------------------------------------------------------


def test_pre_llm_call_returns_none_when_solomon_off(solomon_home: Path):
    profile.init_solomon_home()
    (solomon_home / ".solomon_off").touch()
    result = hooks.pre_llm_call(session_id="s", user_message="x",
                                   conversation_history=[], is_first_turn=True,
                                   model="m", platform="cli")
    assert result is None


def test_pre_llm_call_returns_none_for_private_session(solomon_home: Path):
    profile.init_solomon_home()
    session_state.mark_private("private-sess")
    result = hooks.pre_llm_call(session_id="private-sess", user_message="x",
                                   conversation_history=[], is_first_turn=False,
                                   model="m", platform="telegram")
    assert result is None


# ---------------------------------------------------------------------------
# Inbound detection
# ---------------------------------------------------------------------------


def test_pre_llm_call_flags_inbound_for_gateway_platforms(solomon_home: Path):
    profile.init_solomon_home()
    block = hooks.pre_llm_call(
        session_id="s",
        user_message={"message_id": "tg-42", "text": "vendor email"},
        conversation_history=[], is_first_turn=False,
        model="m", platform="telegram",
    )["context"]
    assert "INBOUND CONTEXT" in block
    assert "telegram" in block


def test_pre_llm_call_does_not_flag_cli_as_inbound(solomon_home: Path):
    profile.init_solomon_home()
    block = hooks.pre_llm_call(session_id="s", user_message="hi there",
                                  conversation_history=[], is_first_turn=False,
                                  model="m", platform="cli")["context"]
    assert "INBOUND CONTEXT" not in block


# ---------------------------------------------------------------------------
# Active modes — onboarding / mentoring
# ---------------------------------------------------------------------------


def test_pre_llm_call_onboarding_mode_loads_interview_skill(solomon_home: Path):
    profile.init_solomon_home()
    session_state.set_active_mode("s", "onboarding", session_n=0)
    block = hooks.pre_llm_call(session_id="s", user_message="my business is...",
                                  conversation_history=[], is_first_turn=False,
                                  model="m", platform="cli")["context"]
    assert "MODE: onboarding" in block
    assert "SESSION: 0" in block
    assert "Industry & sector" in block
    assert "business_category" in block


def test_pre_llm_call_mentoring_mode_loads_interview_skill(solomon_home: Path):
    profile.init_solomon_home()
    session_state.set_active_mode("s", "mentoring", queue_count=4, action_count=2)
    block = hooks.pre_llm_call(session_id="s", user_message="ok",
                                  conversation_history=[], is_first_turn=False,
                                  model="m", platform="cli")["context"]
    assert "MODE: mentoring" in block
    assert "REVIEW QUEUE PENDING: 4" in block
    assert "ACTIONS NEEDING ATTENTION: 2" in block


# ---------------------------------------------------------------------------
# post_llm_call and on_session_start logging
# ---------------------------------------------------------------------------


def test_post_llm_call_logs_turn_end(solomon_home: Path):
    profile.init_solomon_home()
    hooks.post_llm_call(session_id="sess", model="m", platform="cli")
    from solomon import logs
    text = logs.log_path().read_text()
    assert "turn_end" in text


def test_post_llm_call_private_only_logs_private_turn(solomon_home: Path):
    profile.init_solomon_home()
    session_state.mark_private("priv")
    hooks.post_llm_call(session_id="priv", model="m", platform="telegram")
    from solomon import logs
    text = logs.log_path().read_text()
    assert "private_turn" in text


def test_on_session_start_logs(solomon_home: Path):
    profile.init_solomon_home()
    hooks.on_session_start(session_id="sess-x")
    from solomon import logs
    text = logs.log_path().read_text()
    assert "session_start" in text


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_all_attaches_three_hooks(solomon_home: Path):
    calls = []

    class FakeAdapter:
        def register_hook(self, hook_name, callback):
            calls.append((hook_name, callback))

    hooks.register_all(FakeAdapter())
    assert [name for name, _ in calls] == [
        "pre_llm_call", "post_llm_call", "on_session_start",
    ]

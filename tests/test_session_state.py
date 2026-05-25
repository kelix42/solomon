"""Tests for active_modes.jsonl and private_sessions.jsonl management."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import json

from solomon import profile, session_state


def test_set_and_get_active_mode(solomon_home: Path):
    profile.init_solomon_home()
    session_state.set_active_mode("s1", "onboarding", session_n=0)
    entry = session_state.get_active_mode("s1")
    assert entry is not None
    assert entry["mode"] == "onboarding"
    assert entry["session_n"] == 0


def test_get_active_mode_returns_none_when_missing(solomon_home: Path):
    profile.init_solomon_home()
    assert session_state.get_active_mode("s-nonexistent") is None


def test_clear_active_mode(solomon_home: Path):
    profile.init_solomon_home()
    session_state.set_active_mode("s2", "mentoring")
    assert session_state.clear_active_mode("s2") is True
    assert session_state.get_active_mode("s2") is None


def test_set_active_mode_overwrites_existing(solomon_home: Path):
    profile.init_solomon_home()
    session_state.set_active_mode("s3", "onboarding", session_n=0)
    session_state.set_active_mode("s3", "mentoring")
    entry = session_state.get_active_mode("s3")
    assert entry["mode"] == "mentoring"
    # The earlier onboarding entry shouldn't bleed through.
    assert "session_n" not in entry


def test_stale_entries_ignored_after_6h(solomon_home: Path):
    profile.init_solomon_home()
    # Manually write a stale entry with started_at 7h ago.
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    path = solomon_home / "active_modes.jsonl"
    path.write_text(json.dumps({
        "session_id": "s-stale",
        "mode": "onboarding",
        "session_n": 0,
        "started_at": stale_ts,
    }) + "\n", encoding="utf-8")
    # Read should treat this as no active mode.
    assert session_state.get_active_mode("s-stale") is None


def test_fresh_entries_not_ignored(solomon_home: Path):
    profile.init_solomon_home()
    fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    path = solomon_home / "active_modes.jsonl"
    path.write_text(json.dumps({
        "session_id": "s-fresh",
        "mode": "mentoring",
        "started_at": fresh_ts,
    }) + "\n", encoding="utf-8")
    entry = session_state.get_active_mode("s-fresh")
    assert entry is not None
    assert entry["mode"] == "mentoring"


def test_multiple_sessions_dont_collide(solomon_home: Path):
    profile.init_solomon_home()
    session_state.set_active_mode("s-a", "onboarding", session_n=0)
    session_state.set_active_mode("s-b", "mentoring")
    a = session_state.get_active_mode("s-a")
    b = session_state.get_active_mode("s-b")
    assert a["mode"] == "onboarding"
    assert b["mode"] == "mentoring"


def test_mark_private_then_is_private(solomon_home: Path):
    profile.init_solomon_home()
    assert session_state.is_private("p1") is False
    session_state.mark_private("p1")
    assert session_state.is_private("p1") is True


def test_unmark_private(solomon_home: Path):
    profile.init_solomon_home()
    session_state.mark_private("p2")
    assert session_state.unmark_private("p2") is True
    assert session_state.is_private("p2") is False


def test_unmark_unknown_returns_false(solomon_home: Path):
    profile.init_solomon_home()
    assert session_state.unmark_private("nope") is False


def test_list_private_session_ids(solomon_home: Path):
    profile.init_solomon_home()
    session_state.mark_private("a")
    session_state.mark_private("b")
    session_state.mark_private("c")
    ids = session_state.list_private_session_ids()
    assert ids == {"a", "b", "c"}


def test_mark_private_idempotent(solomon_home: Path):
    profile.init_solomon_home()
    session_state.mark_private("x")
    session_state.mark_private("x")
    session_state.mark_private("x")
    assert len(session_state.list_private_session_ids()) == 1


def test_corrupt_jsonl_lines_are_skipped(solomon_home: Path):
    profile.init_solomon_home()
    path = solomon_home / "active_modes.jsonl"
    path.write_text(
        '{"session_id": "ok", "mode": "onboarding", "started_at": "' +
        datetime.now(timezone.utc).isoformat() + '"}\n'
        'this is not json\n'
        '{"another": "bad"}\n',
        encoding="utf-8",
    )
    # No exception, and the good entry is found.
    entry = session_state.get_active_mode("ok")
    assert entry and entry["mode"] == "onboarding"

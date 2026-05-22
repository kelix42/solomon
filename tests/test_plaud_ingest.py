"""Tests for solomon.workers.plaud_ingest (state + helpers; worker stub)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from solomon.workers import plaud_ingest as pi


# ---------------------------------------------------------------------------
# State load / save
# ---------------------------------------------------------------------------


def test_load_state_empty_returns_defaults(solomon_db):
    s = pi.load_state()
    assert s.last_seen_uid is None
    assert s.recent_email_ids == []
    assert s.consecutive_fails == 0


def test_save_and_load_roundtrip(solomon_db):
    s = pi.PlaudState(
        last_seen_uid=42,
        recent_email_ids=["a", "b", "c"],
        last_idle_at="2026-01-15T10:00:00Z",
        last_poll_at="2026-01-15T10:01:00Z",
        consecutive_fails=2,
    )
    pi.save_state(s)
    out = pi.load_state()
    assert out.last_seen_uid == 42
    assert out.recent_email_ids == ["a", "b", "c"]
    assert out.last_idle_at == "2026-01-15T10:00:00Z"
    assert out.consecutive_fails == 2


def test_save_state_truncates_recent_buffer(solomon_db):
    s = pi.PlaudState(
        last_seen_uid=1,
        recent_email_ids=[str(i) for i in range(pi.PLAUD_RECENT_BUFFER_MAX + 50)],
    )
    pi.save_state(s)
    out = pi.load_state()
    assert len(out.recent_email_ids) == pi.PLAUD_RECENT_BUFFER_MAX
    # Tail preserved.
    assert out.recent_email_ids[-1] == str(pi.PLAUD_RECENT_BUFFER_MAX + 49)


def test_save_state_is_idempotent(solomon_db):
    s = pi.PlaudState(last_seen_uid=7)
    pi.save_state(s)
    pi.save_state(s)
    pi.save_state(s)
    # Still one row.
    out = pi.load_state()
    assert out.last_seen_uid == 7


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_from_env_returns_none_when_missing(monkeypatch):
    for k in (
        "SOLOMON_PLAUD_IMAP_HOST",
        "SOLOMON_PLAUD_IMAP_USER",
        "SOLOMON_PLAUD_IMAP_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)
    assert pi.PlaudConfig.from_env() is None


def test_config_from_env_populated(monkeypatch):
    monkeypatch.setenv("SOLOMON_PLAUD_IMAP_HOST", "imap.gmail.com")
    monkeypatch.setenv("SOLOMON_PLAUD_IMAP_USER", "you@example.com")
    monkeypatch.setenv("SOLOMON_PLAUD_IMAP_PASSWORD", "app-password")
    monkeypatch.setenv("SOLOMON_PLAUD_IMAP_PORT", "143")
    monkeypatch.setenv("SOLOMON_PLAUD_IMAP_SSL", "0")
    cfg = pi.PlaudConfig.from_env()
    assert cfg is not None
    assert cfg.imap_host == "imap.gmail.com"
    assert cfg.imap_port == 143
    assert cfg.imap_use_ssl is False


# ---------------------------------------------------------------------------
# save_attachment
# ---------------------------------------------------------------------------


def test_save_attachment_writes_iso_prefixed(tmp_path, monkeypatch):
    when = datetime(2026, 1, 15, 10, 30, 0)
    p = pi.save_attachment(
        content="hello transcript",
        original_filename="meeting notes.txt",
        received_at=when,
        inbox_root=tmp_path / "corpus" / "inbox",
    )
    assert p.exists()
    # ISO-stamped name + safe filename.
    assert p.name.startswith("20260115T103000Z-")
    assert p.read_text(encoding="utf-8") == "hello transcript"
    # Lives under messages/.
    assert p.parent.name == "messages"


def test_save_attachment_appends_txt_when_missing(tmp_path):
    p = pi.save_attachment(
        content="x",
        original_filename="weird",
        inbox_root=tmp_path / "corpus" / "inbox",
    )
    assert p.name.endswith("weird.txt")


def test_save_attachment_sanitises_path_chars(tmp_path):
    p = pi.save_attachment(
        content="x",
        original_filename="../../etc/passwd",
        inbox_root=tmp_path / "corpus" / "inbox",
    )
    assert ".." not in p.name
    assert p.parent.name == "messages"


# ---------------------------------------------------------------------------
# Stub entry point
# ---------------------------------------------------------------------------


def test_main_returns_1_without_creds(monkeypatch):
    for k in (
        "SOLOMON_PLAUD_IMAP_HOST",
        "SOLOMON_PLAUD_IMAP_USER",
        "SOLOMON_PLAUD_IMAP_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)
    assert pi.main() == 1


def test_main_returns_0_with_creds(monkeypatch):
    monkeypatch.setenv("SOLOMON_PLAUD_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("SOLOMON_PLAUD_IMAP_USER", "u")
    monkeypatch.setenv("SOLOMON_PLAUD_IMAP_PASSWORD", "p")
    # The stub returns 0 with a warning; doesn't actually open IMAP.
    assert pi.main() == 0

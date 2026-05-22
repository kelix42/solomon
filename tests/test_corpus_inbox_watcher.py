"""Tests for the corpus inbox watcher.

We exercise the pure helpers (path skip, file-stable check, catch-up scan)
plus a synthetic end-to-end through ``InboxWatcher.queue → drain`` with a
mocked ``ingest_fn``. The blocking watchdog loop in ``run()`` is
out-of-scope; it's covered by the smoke test in the README during install.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from solomon.workers.corpus_inbox_watcher import (
    InboxWatcher,
    _is_file_stable,
    _should_skip_path,
    catch_up_scan,
)


# ---------------------------------------------------------------------------
# _should_skip_path
# ---------------------------------------------------------------------------


def test_should_skip_directories(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    sub = inbox / "docs"
    sub.mkdir()
    assert _should_skip_path(sub, inbox_root=inbox) is True


def test_should_skip_hidden_files(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    hidden = inbox / ".hidden.txt"
    hidden.write_text("x")
    assert _should_skip_path(hidden, inbox_root=inbox) is True


def test_should_skip_parking_dirs(tmp_path):
    inbox = tmp_path / "inbox"
    parked = inbox / "_unsupported"
    parked.mkdir(parents=True)
    f = parked / "x.txt"
    f.write_text("x")
    assert _should_skip_path(f, inbox_root=inbox) is True


def test_should_not_skip_normal_file(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    f = inbox / "x.txt"
    f.write_text("x")
    assert _should_skip_path(f, inbox_root=inbox) is False


def test_should_skip_outside_inbox(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    outside = tmp_path / "elsewhere" / "y.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("x")
    assert _should_skip_path(outside, inbox_root=inbox) is True


# ---------------------------------------------------------------------------
# _is_file_stable
# ---------------------------------------------------------------------------


def test_is_file_stable_size_unchanged(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("hello")
    assert _is_file_stable(p, stable_seconds=0.0, sleeper=lambda _: None) is True


def test_is_file_stable_size_changes(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("hello")
    def grow(_seconds):
        p.write_text("hello-and-more")
    assert _is_file_stable(p, stable_seconds=0.0, sleeper=grow) is False


def test_is_file_stable_disappears(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("hello")
    def unlink(_seconds):
        p.unlink()
    assert _is_file_stable(p, stable_seconds=0.0, sleeper=unlink) is False


def test_is_file_stable_missing_from_start(tmp_path):
    p = tmp_path / "does-not-exist.txt"
    assert _is_file_stable(p, stable_seconds=0.0, sleeper=lambda _: None) is False


# ---------------------------------------------------------------------------
# catch_up_scan
# ---------------------------------------------------------------------------


def test_catch_up_scan_finds_files(tmp_path):
    inbox = tmp_path / "corpus" / "inbox"
    (inbox / "docs").mkdir(parents=True)
    (inbox / "docs" / "a.txt").write_text("a")
    (inbox / "docs" / "b.md").write_text("b")
    # Parking dir — must not be picked up.
    parked = inbox / "_unsupported"
    parked.mkdir()
    (parked / "skip-me.txt").write_text("ignore")
    files = catch_up_scan(inbox_root=inbox)
    names = {f.name for f in files}
    assert "a.txt" in names
    assert "b.md" in names
    assert "skip-me.txt" not in names


def test_catch_up_scan_missing_inbox(tmp_path):
    assert catch_up_scan(inbox_root=tmp_path / "no-such") == []


# ---------------------------------------------------------------------------
# InboxWatcher queue → drain
# ---------------------------------------------------------------------------


def _fake_result(path):
    """Minimal stand-in for an IngestResult."""
    class _R:
        def __init__(self, p):
            self.status = "success"
            self.raw_path = str(p)
    return _R(path)


def test_watcher_queue_dedupes(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    p = inbox / "x.txt"
    p.write_text("body")
    w = InboxWatcher(inbox_root=inbox, ingest_fn=_fake_result)
    w.queue(p)
    w.queue(p)
    w.queue(p)
    assert len(w._pending) == 1


def test_watcher_drain_calls_ingest_for_each(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "solomon.workers.corpus_inbox_watcher._is_file_stable",
        lambda p, **kw: True,
    )
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    a = inbox / "a.txt"; a.write_text("hi")
    b = inbox / "b.txt"; b.write_text("yo")

    calls = []
    def ingest_fn(p):
        calls.append(p)
        return _fake_result(p)

    w = InboxWatcher(inbox_root=inbox, ingest_fn=ingest_fn)
    w.queue(a)
    w.queue(b)
    results = w.drain()
    assert len(results) == 2
    assert {p.name for p in calls} == {"a.txt", "b.txt"}
    assert w._pending == set()


def test_watcher_drain_skips_unstable_files(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "solomon.workers.corpus_inbox_watcher._is_file_stable",
        lambda p, **kw: False,
    )
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    a = inbox / "a.txt"; a.write_text("hi")

    calls = []
    def ingest_fn(p):
        calls.append(p)
        return _fake_result(p)

    w = InboxWatcher(inbox_root=inbox, ingest_fn=ingest_fn)
    w.queue(a)
    results = w.drain()
    assert results == []
    assert calls == []


def test_watcher_drain_swallows_ingest_exceptions(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "solomon.workers.corpus_inbox_watcher._is_file_stable",
        lambda p, **kw: True,
    )
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    a = inbox / "a.txt"; a.write_text("hi")
    b = inbox / "b.txt"; b.write_text("yo")

    def ingest_fn(p):
        if p.name == "a.txt":
            raise RuntimeError("boom")
        return _fake_result(p)

    w = InboxWatcher(inbox_root=inbox, ingest_fn=ingest_fn)
    w.queue(a)
    w.queue(b)
    results = w.drain()
    assert len(results) == 1  # only b succeeded; a's exception swallowed
    assert results[0].raw_path == str(b)


def test_ready_to_drain_respects_debounce(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    p = inbox / "x.txt"; p.write_text("body")

    w = InboxWatcher(inbox_root=inbox, ingest_fn=_fake_result)
    # Empty pending: never ready.
    assert w._ready_to_drain() is False
    w.queue(p)
    # Just-queued: not enough time elapsed unless we've hit the reset cap.
    # Fake "right now" with a small monkey-patch of time.time inside the module.
    import solomon.workers.corpus_inbox_watcher as cw

    monkeypatch.setattr(cw.time, "time", lambda: w._last_event_at + 1.0)
    assert w._ready_to_drain() is False
    monkeypatch.setattr(cw.time, "time", lambda: w._last_event_at + 31.0)
    assert w._ready_to_drain() is True


def test_reset_cap_triggers_drain(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    w = InboxWatcher(inbox_root=inbox, ingest_fn=_fake_result)
    for i in range(7):
        p = inbox / f"x{i}.txt"
        p.write_text("body")
        w.queue(p)
    # More than DEBOUNCE_MAX_RESETS queue calls — drain is ready right away.
    assert w._ready_to_drain() is True

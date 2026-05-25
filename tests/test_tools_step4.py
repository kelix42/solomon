"""Step-4 tools: dedupe, cron-side I/O, compression, nudge cadence, send_to_owner."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from solomon import profile, tools


# ---------------------------------------------------------------------------
# Dedupe in propose_addition
# ---------------------------------------------------------------------------


def test_propose_addition_dedupes_identical_pending(solomon_home: Path):
    profile.init_solomon_home()
    iid1 = tools.propose_addition(
        file="finance", section="Pricing", content="No discount > 15%.",
        reason="from chat A",
    )
    iid2 = tools.propose_addition(
        file="finance", section="Pricing", content="No discount > 15%.",
        reason="from chat B",
    )
    assert iid1 == iid2
    items = tools.read_queue(status="pending")
    # Only one item written despite two propose calls.
    assert sum(1 for it in items if it["id"] == iid1) == 1


def test_propose_addition_no_dedupe_after_decision(solomon_home: Path):
    profile.init_solomon_home()
    iid = tools.propose_addition(
        file="finance", section="Pricing", content="No discount > 15%.",
        reason="from chat",
    )
    tools.apply_queue_decision(iid, "approve")
    iid2 = tools.propose_addition(
        file="finance", section="Pricing", content="No discount > 15%.",
        reason="re-captured later",
    )
    # The earlier item is no longer "pending" — new one is created.
    assert iid2 != iid


def test_propose_addition_dedupe_per_file_section_content(solomon_home: Path):
    profile.init_solomon_home()
    a = tools.propose_addition(file="finance", section="Pricing",
                                 content="rule A", reason="x")
    b = tools.propose_addition(file="finance", section="Pricing",
                                 content="rule B", reason="x")  # different content
    c = tools.propose_addition(file="finance", section="Margins",
                                 content="rule A", reason="x")  # different section
    d = tools.propose_addition(file="customers", section="Pricing",
                                 content="rule A", reason="x")  # different file
    assert len({a, b, c, d}) == 4


# ---------------------------------------------------------------------------
# Compression tools
# ---------------------------------------------------------------------------


def test_propose_compression_queues_item(solomon_home: Path):
    profile.init_solomon_home()
    iid = tools.propose_compression(
        file="finance",
        content="# Finance\n\nShorter content.\n",
        summary="dropped 3 redundant statements",
        diff="--- old\n+++ new\n",
    )
    assert iid.startswith("q_")
    items = profile.read_queue("review", status="pending", limit=100)
    found = next(it for it in items if it["id"] == iid)
    assert found["kind"] == "compression"
    assert found["file"] == "finance"
    assert found["reason"] == "dropped 3 redundant statements"


def test_propose_compression_validates_file(solomon_home: Path):
    profile.init_solomon_home()
    with pytest.raises(ValueError):
        tools.propose_compression(file="not_a_playbook",
                                    content="x", summary="y")


def test_apply_profile_summary(solomon_home: Path):
    profile.init_solomon_home()
    assert tools.apply_profile_summary("This is the new summary.") is True
    import yaml
    data = yaml.safe_load((solomon_home / "profile.yaml").read_text())
    assert data["summary"]["text"] == "This is the new summary."
    assert data["summary"]["generated_at"]


def test_apply_profile_summary_requires_text(solomon_home: Path):
    profile.init_solomon_home()
    with pytest.raises(ValueError):
        tools.apply_profile_summary("")


# ---------------------------------------------------------------------------
# Inbox / archive
# ---------------------------------------------------------------------------


def test_list_inbox_empty(solomon_home: Path):
    profile.init_solomon_home()
    assert tools.list_inbox() == []


def test_list_inbox_returns_filenames(solomon_home: Path):
    profile.init_solomon_home()
    (solomon_home / "inbox" / "a.txt").write_text("a")
    (solomon_home / "inbox" / "b.eml").write_text("b")
    assert sorted(tools.list_inbox()) == ["a.txt", "b.eml"]


def test_read_inbox_file_returns_content(solomon_home: Path):
    profile.init_solomon_home()
    (solomon_home / "inbox" / "doc.txt").write_text("hello there")
    assert tools.read_inbox_file("doc.txt") == "hello there"


def test_read_inbox_file_rejects_path_traversal(solomon_home: Path):
    profile.init_solomon_home()
    with pytest.raises(ValueError):
        tools.read_inbox_file("../../etc/passwd")
    with pytest.raises(ValueError):
        tools.read_inbox_file("subdir/file.txt")


def test_read_inbox_file_truncates_large(solomon_home: Path):
    profile.init_solomon_home()
    (solomon_home / "inbox" / "big.txt").write_text("x" * 100_000)
    out = tools.read_inbox_file("big.txt", max_chars=1000)
    assert "TRUNCATED" in out
    assert len(out) < 100_000


def test_archive_file_processed(solomon_home: Path):
    profile.init_solomon_home()
    (solomon_home / "inbox" / "doc.txt").write_text("x")
    dest = tools.archive_file("doc.txt", status="processed")
    assert not (solomon_home / "inbox" / "doc.txt").exists()
    assert Path(dest).exists()
    assert "processed" in dest


def test_archive_file_failed_writes_error_note(solomon_home: Path):
    profile.init_solomon_home()
    (solomon_home / "inbox" / "bad.txt").write_text("x")
    dest = tools.archive_file("bad.txt", status="failed",
                                error="LLM call timed out")
    assert Path(dest).exists()
    err_file = Path(dest).parent / (Path(dest).name + ".error.txt")
    assert err_file.exists()
    assert "timed out" in err_file.read_text()


def test_archive_file_idempotent(solomon_home: Path):
    profile.init_solomon_home()
    (solomon_home / "inbox" / "doc.txt").write_text("x")
    tools.archive_file("doc.txt", status="processed")
    # Second call when the source is gone should return the would-be path
    # without raising.
    result = tools.archive_file("doc.txt", status="processed")
    assert result  # returns a path


def test_archive_file_bad_status(solomon_home: Path):
    profile.init_solomon_home()
    with pytest.raises(ValueError):
        tools.archive_file("anything.txt", status="weird")


# ---------------------------------------------------------------------------
# Nudge cadence
# ---------------------------------------------------------------------------


def _make_pending_action(home: Path, *, urgency: str, notified_hours_ago: float = 5,
                          nudge_count: int = 0,
                          last_nudge_hours_ago: float = None) -> str:
    """Helper: seed a pending_actions item with specific timing."""
    item_id = profile.append_action_item({
        "source_kind": "email",
        "source_id": f"<{urgency}-{notified_hours_ago}>",
        "source_summary": "x",
        "first_pass_prediction": "x",
        "final_recommendation": "x",
        "reasoning": "x",
        "urgency": urgency,
        "action_kind": "record_only",
    })
    now = datetime.now(timezone.utc)
    notified_at = (now - timedelta(hours=notified_hours_ago)).isoformat()
    updates = {"owner_notified_at": notified_at, "nudge_count": nudge_count}
    if last_nudge_hours_ago is not None:
        updates["last_nudge_at"] = (now - timedelta(hours=last_nudge_hours_ago)).isoformat()
    profile.update_queue_item("actions", item_id, updates)
    return item_id


def test_list_due_for_nudge_high_urgency(solomon_home: Path):
    profile.init_solomon_home()
    # high urgency, notified 2h ago, never nudged → due (interval = 1h)
    iid = _make_pending_action(solomon_home, urgency="high",
                                notified_hours_ago=2)
    due = tools.list_pending_actions_due_for_nudge()
    assert any(it["id"] == iid for it in due)


def test_list_due_for_nudge_too_recent(solomon_home: Path):
    profile.init_solomon_home()
    # high urgency, notified 0.5h ago → not yet due (interval = 1h)
    _make_pending_action(solomon_home, urgency="high",
                          notified_hours_ago=0.5)
    due = tools.list_pending_actions_due_for_nudge()
    assert due == []


def test_list_due_for_nudge_max_reached(solomon_home: Path):
    profile.init_solomon_home()
    _make_pending_action(solomon_home, urgency="high",
                          notified_hours_ago=20, nudge_count=3)
    due = tools.list_pending_actions_due_for_nudge()
    assert due == []


def test_send_nudge_respects_cadence(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    sent_log: list = []

    class FakeAdapter:
        def send_to_owner(self, text, target=None):
            sent_log.append((text, target))
            return True

    monkeypatch.setattr(tools, "_adapter", FakeAdapter())

    # nudged 30 min ago, high urgency → too soon
    iid = _make_pending_action(solomon_home, urgency="high",
                                notified_hours_ago=10,
                                nudge_count=1, last_nudge_hours_ago=0.5)
    result = tools.send_nudge(iid, "ping")
    assert result is False
    assert not sent_log


def test_send_nudge_success(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    sent_log = []

    class FakeAdapter:
        def send_to_owner(self, text, target=None):
            sent_log.append((text, target))
            return True

    monkeypatch.setattr(tools, "_adapter", FakeAdapter())
    iid = _make_pending_action(solomon_home, urgency="high",
                                notified_hours_ago=5)
    assert tools.send_nudge(iid, "still waiting?") is True
    assert sent_log[0][0] == "still waiting?"
    item = profile.find_queue_item("actions", iid)
    assert item["nudge_count"] == 1
    assert item["last_nudge_at"]


def test_send_nudge_marks_stale_after_max(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    class OkAdapter:
        def send_to_owner(self, text, target=None):
            return True

    monkeypatch.setattr(tools, "_adapter", OkAdapter())
    iid = _make_pending_action(solomon_home, urgency="high",
                                notified_hours_ago=20,
                                nudge_count=2, last_nudge_hours_ago=2)
    tools.send_nudge(iid, "last call")
    item = profile.find_queue_item("actions", iid)
    assert item["nudge_count"] == 3
    assert item["status"] == "stale"


def test_send_nudge_queues_on_send_failure(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    class FailingAdapter:
        def send_to_owner(self, text, target=None):
            return False

    monkeypatch.setattr(tools, "_adapter", FailingAdapter())
    iid = _make_pending_action(solomon_home, urgency="medium",
                                notified_hours_ago=6)
    assert tools.send_nudge(iid, "hi") is False
    # Pending message file should exist with the nudge text queued.
    pending = solomon_home / "pending_messages.jsonl"
    assert pending.exists()
    assert "hi" in pending.read_text()
    # nudge_count NOT incremented on failure — so the next cron retries.
    item = profile.find_queue_item("actions", iid)
    assert item.get("nudge_count", 0) == 0


# ---------------------------------------------------------------------------
# send_to_owner + retry_pending_messages
# ---------------------------------------------------------------------------


def test_send_to_owner_success(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    sent = []

    class A:
        def send_to_owner(self, text, target=None):
            sent.append((text, target))
            return True

    monkeypatch.setattr(tools, "_adapter", A())
    assert tools.send_to_owner("hello", target="telegram:42") is True
    assert sent[0] == ("hello", "telegram:42")


def test_send_to_owner_queues_on_failure(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    class Fail:
        def send_to_owner(self, text, target=None):
            return False

    monkeypatch.setattr(tools, "_adapter", Fail())
    tools.send_to_owner("oops")
    assert (solomon_home / "pending_messages.jsonl").exists()


def test_retry_pending_messages(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    # Seed a pending message manually.
    (solomon_home / "pending_messages.jsonl").write_text(
        json.dumps({"ts": "2026-05-25T00:00:00Z",
                    "text": "queued msg", "target": "telegram:1"}) + "\n",
        encoding="utf-8",
    )
    sent = []

    class A:
        def send_to_owner(self, text, target=None):
            sent.append((text, target))
            return True

    monkeypatch.setattr(tools, "_adapter", A())
    count = tools.retry_pending_messages()
    assert count == 1
    assert sent[0] == ("queued msg", "telegram:1")
    assert not (solomon_home / "pending_messages.jsonl").exists()


def test_retry_pending_messages_partial_failure(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    (solomon_home / "pending_messages.jsonl").write_text(
        json.dumps({"ts": "T1", "text": "a", "target": "t:1"}) + "\n" +
        json.dumps({"ts": "T2", "text": "b", "target": "t:1"}) + "\n",
        encoding="utf-8",
    )

    class FailEveryOther:
        def __init__(self):
            self.calls = 0

        def send_to_owner(self, text, target=None):
            self.calls += 1
            return self.calls % 2 == 0  # second call succeeds

    monkeypatch.setattr(tools, "_adapter", FailEveryOther())
    count = tools.retry_pending_messages()
    assert count == 1
    # One entry remains.
    assert (solomon_home / "pending_messages.jsonl").exists()
    lines = (solomon_home / "pending_messages.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


# ---------------------------------------------------------------------------
# read_conversations excludes private sessions
# ---------------------------------------------------------------------------


def test_read_conversations_excludes_private(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    from solomon import session_state
    session_state.mark_private("s-priv")

    captured_excluded = {}

    class A:
        def read_conversations(self, *, since, limit, exclude_session_ids):
            captured_excluded["x"] = exclude_session_ids
            return []

    monkeypatch.setattr(tools, "_adapter", A())
    tools.read_conversations()
    assert "s-priv" in (captured_excluded["x"] or set())


def test_read_conversations_no_adapter(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    monkeypatch.setattr(tools, "_adapter", None)
    assert tools.read_conversations() == []


# ---------------------------------------------------------------------------
# register_all now registers 19 tools and stores adapter
# ---------------------------------------------------------------------------


def test_register_all_registers_all_19_tools(solomon_home: Path):
    profile.init_solomon_home()
    calls = []

    class FakeAdapter:
        def register_tool(self, *, name, description, schema, handler):
            calls.append(name)

    tools.register_all(FakeAdapter())
    assert set(calls) == {
        "read_profile", "read_playbook", "read_queue",
        "propose_addition", "flag_contradiction",
        "propose_action", "note_handled",
        "apply_queue_decision", "mark_session_complete",
        "propose_compression", "apply_profile_summary",
        "list_inbox", "read_inbox_file", "archive_file",
        "read_conversations",
        "list_pending_actions_due_for_nudge", "send_nudge",
        "send_to_owner", "retry_pending_messages",
    }


def test_register_all_stores_adapter_at_module_level(solomon_home: Path):
    profile.init_solomon_home()

    class FakeAdapter:
        def register_tool(self, **kw):
            pass

    a = FakeAdapter()
    tools.register_all(a)
    assert tools._adapter is a

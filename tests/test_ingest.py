"""Tests for ingest.py — document processing with a fake adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from solomon import ingest, profile, tools


class FakeAdapter:
    """A LLM stub that, when asked, calls a configured tool with given args."""

    def __init__(self, on_call=None):
        self.on_call = on_call
        self.calls: list[tuple[str, list[dict]]] = []

    def llm_call(self, *, system: str, messages: list[dict],
                  json_mode: bool = False, max_tokens: int = 2048) -> str:
        self.calls.append((system, messages))
        if self.on_call:
            self.on_call(system, messages)
        return "ok"


def test_process_file_no_adapter_archives(solomon_home: Path):
    profile.init_solomon_home()
    f = solomon_home / "inbox" / "doc.txt"
    f.write_text("some content")
    result = ingest.process_file(f, adapter=None)
    assert result["ok"]
    assert not f.exists()
    # Moved to processed.
    processed = list((solomon_home / "archive" / "processed").rglob("doc.txt"))
    assert len(processed) == 1


def test_process_file_with_adapter_makes_proposals(solomon_home: Path):
    profile.init_solomon_home()
    f = solomon_home / "inbox" / "policy.txt"
    f.write_text("We never discount over 15%.")

    # The LLM "decides" to propose an addition. Use a side-effecting fake.
    def on_call(system, messages):
        tools.propose_addition(
            file="finance",
            section="Pricing discipline",
            content="Discounts cap at 15%.",
            reason="From 'policy.txt', stated on page 1.",
        )

    adapter = FakeAdapter(on_call=on_call)
    result = ingest.process_file(f, adapter=adapter)
    assert result["ok"]
    assert result["proposals"] == 1
    items = profile.read_queue("review", status="pending")
    assert any("policy.txt" in (it.get("reason") or "") for it in items)


def test_process_file_llm_failure_moves_to_failed(solomon_home: Path):
    profile.init_solomon_home()
    f = solomon_home / "inbox" / "broken.txt"
    f.write_text("anything")

    class FailingAdapter:
        def llm_call(self, **kwargs):
            raise RuntimeError("LLM exploded")

    result = ingest.process_file(f, adapter=FailingAdapter())
    assert not result["ok"]
    assert not f.exists()
    failed = list((solomon_home / "archive" / "failed").rglob("broken.txt"))
    assert len(failed) == 1
    # Error note should be present.
    err = next((solomon_home / "archive" / "failed").rglob("broken.txt.error.txt"))
    assert "LLM" in err.read_text()


def test_process_all_handles_empty_inbox(solomon_home: Path):
    profile.init_solomon_home()
    summary = ingest.process_all(adapter=None)
    assert summary == {"ok": 0, "failed": 0, "proposals": 0}


def test_process_all_processes_multiple(solomon_home: Path):
    profile.init_solomon_home()
    (solomon_home / "inbox" / "a.txt").write_text("aaa")
    (solomon_home / "inbox" / "b.txt").write_text("bbb")
    summary = ingest.process_all(adapter=None)
    assert summary["ok"] == 2
    assert summary["failed"] == 0


def test_chunking_long_document(solomon_home: Path):
    profile.init_solomon_home()
    long = ("paragraph\n\n" * 4000)  # ~52k chars
    f = solomon_home / "inbox" / "long.txt"
    f.write_text(long)
    adapter = FakeAdapter()
    result = ingest.process_file(f, adapter=adapter)
    assert result["ok"]
    # Multiple LLM calls (chunks).
    assert len(adapter.calls) >= 1


def test_unsupported_extension_falls_back_to_text(solomon_home: Path):
    profile.init_solomon_home()
    f = solomon_home / "inbox" / "doc.unknown"
    f.write_text("plain text content")
    result = ingest.process_file(f, adapter=None)
    # Falls back to UTF-8 read; processes OK.
    assert result["ok"]

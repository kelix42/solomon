"""Tests for the weekly compression cron."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from solomon import profile, weekly


class FakeLLM:
    """Returns scripted JSON responses for compression and plain text for summary."""

    def __init__(self, *, compressed_text: str = None, summary: str = "tight new summary"):
        self.compressed_text = compressed_text
        self.summary = summary
        self.calls = 0
        self.ctx = SimpleNamespace()

    def llm_call(self, *, system, messages, json_mode=False, max_tokens=2048):
        self.calls += 1
        # The compression skill expects JSON {rewritten, summary}.
        # The summary regen expects plain text.
        if "compress" in system.lower() or "playbook:" in (messages[0]["content"] or "").lower():
            text = self.compressed_text or "# X\n\nshorter."
            return json.dumps({
                "rewritten": text,
                "summary": "removed three redundant statements, kept exact owner phrases",
            })
        return self.summary


def _seed_verbose_playbook(home: Path, name: str = "finance"):
    p = home / f"{name}.md"
    p.write_text("# Finance\n\nA very verbose description.\n\n" * 5
                  + "Last updated: 2026-01-01\n\n## See also\n", encoding="utf-8")


def test_weekly_run_no_adapter_returns_zero(solomon_home: Path):
    profile.init_solomon_home()
    summary = weekly.run(adapter=None)
    assert summary["compressed"] == 0


def test_weekly_run_queues_compression_proposals(solomon_home: Path):
    profile.init_solomon_home()
    _seed_verbose_playbook(solomon_home)
    adapter = FakeLLM(compressed_text="# Finance\n\nshort.\n")
    summary = weekly.run(adapter=adapter)
    assert summary["compressed"] >= 1
    queue = profile.read_queue("review", status="pending")
    assert any(it.get("kind") == "compression" for it in queue)


def test_weekly_run_skips_trivial_changes(solomon_home: Path):
    profile.init_solomon_home()

    class EchoLLM:
        """Returns the input playbook unchanged — should always be skipped."""
        ctx = SimpleNamespace()

        def llm_call(self, *, system, messages, **kwargs):
            content = messages[0]["content"]
            # Extract just the "Current content:" section.
            if "Current content:" in content:
                current = content.split("Current content:", 1)[1].strip()
            else:
                current = content
            return json.dumps({"rewritten": current, "summary": "No compression needed."})

    summary = weekly.run(adapter=EchoLLM())
    assert summary["compressed"] == 0
    assert summary["skipped"] >= len(profile.PLAYBOOKS)


def test_weekly_regenerates_summary_when_profile_has_content(solomon_home: Path):
    profile.init_solomon_home()
    profile.write_session_summary(0, {
        "business_category": "real estate law",
        "primary_product_or_service": "title work",
        "customer_orientation": "B2C",
        "geographic_scope": "local",
        "revenue_model": "project",
        "growth_stage": "established",
        "concentration_risk": "low",
    })
    adapter = FakeLLM(summary="real estate law firm, local, project billing")
    summary = weekly.run(adapter=adapter)
    assert summary["summary_regenerated"] is True
    data = yaml.safe_load((solomon_home / "profile.yaml").read_text())
    assert "real estate" in (data["summary"]["text"] or "")


def test_weekly_parse_response_with_fenced_block():
    parsed = weekly._parse_compression_response(
        "Here's the compressed file:\n\n```json\n"
        '{"rewritten": "# X\\n\\ndone.", "summary": "tightened"}\n'
        "```\nthat's all."
    )
    assert parsed and parsed["summary"] == "tightened"


def test_weekly_handles_unparseable_response(solomon_home: Path):
    profile.init_solomon_home()
    _seed_verbose_playbook(solomon_home)

    class UnparseableLLM:
        ctx = SimpleNamespace()

        def llm_call(self, **kwargs):
            return "I cannot do this."

    summary = weekly.run(adapter=UnparseableLLM())
    assert summary["compressed"] == 0
    assert summary["skipped"] >= 1


def test_weekly_lock_skip_when_held(solomon_home: Path):
    profile.init_solomon_home()
    import fcntl
    f = open(weekly._lock_path(), "w")
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    try:
        summary = weekly.run(adapter=None)
        assert summary.get("lock_skipped") is True
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()

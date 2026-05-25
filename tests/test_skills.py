"""Verify all four skill files exist, parse, and have valid YAML front matter."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

SKILL_DIR = Path(__file__).parent.parent / "solomon" / "skills"
EXPECTED = ("solomon-default", "solomon-interview", "solomon-ingest", "solomon-compress")


def _split_front_matter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    assert m, "skill must start with YAML front matter delimited by ---"
    return yaml.safe_load(m.group(1)), m.group(2)


def test_all_four_skills_exist():
    for name in EXPECTED:
        path = SKILL_DIR / f"{name}.md"
        assert path.exists(), f"missing skill: {path}"


def test_each_skill_has_required_front_matter():
    for name in EXPECTED:
        text = (SKILL_DIR / f"{name}.md").read_text()
        meta, body = _split_front_matter(text)
        assert "name" in meta
        assert "description" in meta
        assert "version" in meta
        assert "metadata" in meta
        assert "phase" in meta["metadata"]
        assert isinstance(body, str) and len(body) > 100


def test_default_is_marked_always_load():
    text = (SKILL_DIR / "solomon-default.md").read_text()
    meta, _ = _split_front_matter(text)
    assert meta["metadata"]["always_load"] is True


def test_others_are_not_always_load():
    for name in ("solomon-interview", "solomon-ingest", "solomon-compress"):
        text = (SKILL_DIR / f"{name}.md").read_text()
        meta, _ = _split_front_matter(text)
        assert meta["metadata"]["always_load"] is False


def test_default_contains_two_pass_flow():
    body = (SKILL_DIR / "solomon-default.md").read_text()
    assert "two-pass" in body.lower()
    assert "propose_action" in body
    assert "note_handled" in body


def test_interview_contains_three_modes():
    body = (SKILL_DIR / "solomon-interview.md").read_text()
    for mode in ("Mode A", "Mode B", "Mode C"):
        assert mode in body
    assert "mark_session_complete" in body
    assert "apply_queue_decision" in body

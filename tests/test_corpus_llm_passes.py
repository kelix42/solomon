"""Tests for solomon.corpus.llm_passes and prompts.

The LLM is stubbed via a fake client installed on the singleton in
solomon.reasoning.llm so we exercise envelope parsing, page-write paths,
and the index append logic without hitting an API.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

import solomon.reasoning.llm as solomon_llm
from solomon.corpus import llm_passes as lp
from solomon.corpus.llm_passes import (
    append_index,
    extract,
    merge_pages,
    parse_envelope,
    slug_safe,
    strip_md_fence,
)


# ---------------------------------------------------------------------------
# Stub LLM client
# ---------------------------------------------------------------------------


class _StubLLMClient:
    def __init__(self, scripted_responses: Dict[str, str]) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.scripted = scripted_responses

    @property
    def configured(self) -> bool:
        return True

    def model_for(self, tier: str) -> str:
        return f"stub:{tier}"

    def call(self, *, tier, system, user, json_mode=False, max_tokens=1024, temperature=0.2):
        self.calls.append({"tier": tier, "system": system[:60], "user": user[:60]})
        # Route by what's in the system prompt.
        for key, response in self.scripted.items():
            if key in system:
                return solomon_llm.LLMResponse(text=response, model=f"stub:{tier}")
        return solomon_llm.LLMResponse(text="", model=f"stub:{tier}")


@pytest.fixture
def stub_llm(monkeypatch):
    def _install(scripted):
        stub = _StubLLMClient(scripted)
        monkeypatch.setattr(solomon_llm, "_client", stub)
        return stub
    return _install


@pytest.fixture
def corpus_tmp(tmp_path, monkeypatch):
    """Point the corpus root at tmp_path so page writes land there."""
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    return tmp_path / "corpus"


# ---------------------------------------------------------------------------
# parse_envelope
# ---------------------------------------------------------------------------


def test_parse_envelope_plain_json():
    src = json.dumps({"summary": "x", "entities": [], "concepts": [], "playbooks": [], "proposed_rules": []})
    out = parse_envelope(src)
    assert out["summary"] == "x"
    assert out["entities"] == []


def test_parse_envelope_strips_fences():
    src = "```json\n{\"summary\": \"y\"}\n```"
    out = parse_envelope(src)
    assert out["summary"] == "y"
    assert out["entities"] == []  # defaults filled in


def test_parse_envelope_invalid_returns_empty():
    assert parse_envelope("not json at all") == {}
    assert parse_envelope("") == {}


def test_parse_envelope_fills_missing_keys():
    out = parse_envelope('{"summary": "ok"}')
    assert out["entities"] == []
    assert out["concepts"] == []
    assert out["playbooks"] == []
    assert out["proposed_rules"] == []


def test_parse_envelope_rejects_list():
    assert parse_envelope("[1, 2, 3]") == {}


def test_strip_md_fence():
    assert strip_md_fence("```markdown\nbody\n```") == "body"
    assert strip_md_fence("plain markdown") == "plain markdown"
    assert strip_md_fence("") == ""


def test_slug_safe():
    assert slug_safe("Acme Corp.") == "acme-corp"
    assert slug_safe("FOO_BAR") == "foo-bar"
    assert slug_safe("") == "untitled"
    assert slug_safe("!!!") == "untitled"


# ---------------------------------------------------------------------------
# Pass 1
# ---------------------------------------------------------------------------


def test_extract_routes_through_llm_with_extract_prompt(stub_llm):
    payload = json.dumps({
        "summary": "doc summary",
        "entities": [{"slug": "acme", "subtype": "customer", "display_name": "Acme",
                      "aliases": [], "new_info": "primary contact details"}],
        "concepts": [],
        "playbooks": [],
        "proposed_rules": [],
    })
    stub = stub_llm({"corpus-ingest analyst": payload})
    env = extract("body text here", category="docs", raw_path="corpus/raw/docs/x.txt")
    assert env["summary"] == "doc summary"
    assert env["entities"][0]["slug"] == "acme"
    assert stub.calls and stub.calls[0]["tier"] == "deep"


def test_extract_handles_empty_llm_response(stub_llm):
    stub_llm({"corpus-ingest analyst": ""})
    env = extract("body", category="docs", raw_path="x")
    assert env == {}


# ---------------------------------------------------------------------------
# Pass 2 — merge_pages
# ---------------------------------------------------------------------------


def test_merge_pages_writes_new_page(stub_llm, corpus_tmp):
    page_md = """---
type: entity
subtype: customer
display_name: Acme Corp
aliases: [Acme]
last_updated: 2026-01-15
---
## Identity
Acme Corp, primary customer since 2024.
## Sources
- corpus/raw/docs/x.txt
"""
    stub_llm({"wiki maintainer": page_md})

    envelope = {
        "summary": "x",
        "entities": [{
            "slug": "acme-corp",
            "subtype": "customer",
            "display_name": "Acme Corp",
            "aliases": ["Acme"],
            "new_info": "Primary customer since 2024.",
        }],
        "concepts": [], "playbooks": [], "proposed_rules": [],
    }

    upsert_calls = []
    def fake_upsert(*, page_type, slug, page_path_str, new_content):
        upsert_calls.append((page_type, slug, page_path_str))
        return 3

    touched = merge_pages(
        envelope=envelope,
        raw_path_rel="corpus/raw/docs/x.txt",
        upsert_fn=fake_upsert,
    )
    assert len(touched) == 1
    assert touched[0]["page_type"] == "entity"
    assert touched[0]["slug"] == "acme-corp"
    assert touched[0]["vectors_changed"] == 3

    page_path = corpus_tmp / "wiki" / "entities" / "acme-corp.md"
    assert page_path.exists()
    assert "Acme Corp" in page_path.read_text(encoding="utf-8")
    assert upsert_calls == [("entity", "acme-corp", "corpus/wiki/entities/acme-corp.md")]


def test_merge_pages_skips_empty_new_info(stub_llm, corpus_tmp):
    stub_llm({})
    envelope = {
        "entities": [{"slug": "x", "new_info": ""}],
        "concepts": [], "playbooks": [], "proposed_rules": [],
    }
    upsert_calls = []
    def fake_upsert(**kw):
        upsert_calls.append(kw)
        return 0
    touched = merge_pages(
        envelope=envelope, raw_path_rel="x",
        upsert_fn=fake_upsert,
    )
    assert touched == []
    assert upsert_calls == []


def test_merge_pages_skips_empty_llm_response(stub_llm, corpus_tmp):
    stub_llm({"wiki maintainer": ""})
    envelope = {
        "entities": [{"slug": "x", "new_info": "info"}],
        "concepts": [], "playbooks": [], "proposed_rules": [],
    }
    def fake_upsert(**kw):
        return 0
    touched = merge_pages(envelope=envelope, raw_path_rel="x", upsert_fn=fake_upsert)
    assert touched == []


def test_merge_pages_handles_upsert_exception(stub_llm, corpus_tmp):
    stub_llm({"wiki maintainer": "---\ntype: entity\n---\n## Identity\nstub\n"})
    envelope = {
        "entities": [{"slug": "x", "new_info": "info"}],
        "concepts": [], "playbooks": [], "proposed_rules": [],
    }
    def bad_upsert(**kw):
        raise RuntimeError("pgvector down")
    touched = merge_pages(envelope=envelope, raw_path_rel="x", upsert_fn=bad_upsert)
    # Page still gets written, vectors_changed is 0.
    assert len(touched) == 1
    assert touched[0]["vectors_changed"] == 0


def test_merge_pages_normalises_slug(stub_llm, corpus_tmp):
    stub_llm({"wiki maintainer": "---\ntype: concept\n---\n## Definition\nstub\n"})
    envelope = {
        "entities": [],
        "concepts": [{"slug": "Refund POLICY!", "domain": "ops", "new_info": "info"}],
        "playbooks": [], "proposed_rules": [],
    }
    def fake_upsert(**kw):
        assert kw["slug"] == "refund-policy"
        return 1
    touched = merge_pages(envelope=envelope, raw_path_rel="x", upsert_fn=fake_upsert)
    assert touched[0]["slug"] == "refund-policy"


# ---------------------------------------------------------------------------
# append_index
# ---------------------------------------------------------------------------


def test_append_index_creates_file(corpus_tmp):
    touched = [
        {"slug": "acme", "page_path": "corpus/wiki/entities/acme.md", "page_type": "entity"},
        {"slug": "refunds", "page_path": "corpus/wiki/concepts/refunds.md", "page_type": "concept"},
    ]
    append_index(touched)
    idx_path = corpus_tmp / "index.md"
    text = idx_path.read_text(encoding="utf-8")
    assert "- [acme](corpus/wiki/entities/acme.md) (entity)" in text
    assert "- [refunds](corpus/wiki/concepts/refunds.md) (concept)" in text


def test_append_index_is_idempotent(corpus_tmp):
    touched = [{"slug": "acme", "page_path": "p.md", "page_type": "entity"}]
    append_index(touched)
    append_index(touched)
    text = (corpus_tmp / "index.md").read_text(encoding="utf-8")
    assert text.count("- [acme]") == 1


def test_append_index_empty_input_noop(corpus_tmp):
    append_index([])
    assert not (corpus_tmp / "index.md").exists()

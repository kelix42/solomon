"""Integration tests for solomon.corpus.ingest — the orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

import solomon.reasoning.llm as solomon_llm
from solomon.corpus import embed as ce
from solomon.corpus import ingest as ig
from solomon.corpus import manifest as cm
from solomon.corpus import rules as cr


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def corpus_env(tmp_path, monkeypatch):
    """Wire SOLOMON_CORPUS_ROOT to tmp_path/corpus and stub embeddings + LLM."""
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    (tmp_path / "corpus" / "inbox" / "docs").mkdir(parents=True)
    monkeypatch.setattr(
        "solomon.corpus.embed.embed_batch",
        lambda texts: [[float(len(t)), 0.1, 0.2, 0.3] for t in texts],
    )
    return tmp_path


class _StubLLM:
    """LLM stub that returns scripted JSON for the extract pass and a
    fixed page markdown for the merge pass.
    """

    def __init__(self, envelope: Dict[str, Any], page_md: str = "---\ntype: entity\n---\n## Identity\nstub\n"):
        self.envelope = envelope
        self.page_md = page_md
        self.calls = []

    @property
    def configured(self) -> bool:
        return True

    def model_for(self, tier):
        return "stub"

    def call(self, *, tier, system, user, json_mode=False, max_tokens=1024, temperature=0.2):
        self.calls.append({"tier": tier, "system_snip": system[:50]})
        if "corpus-ingest analyst" in system:
            import json
            return solomon_llm.LLMResponse(text=json.dumps(self.envelope), model="stub")
        if "wiki maintainer" in system:
            return solomon_llm.LLMResponse(text=self.page_md, model="stub")
        return solomon_llm.LLMResponse(text="", model="stub")


def _install_stub_llm(monkeypatch, envelope, page_md=None):
    stub = _StubLLM(envelope, page_md=page_md or "---\ntype: entity\n---\n## Identity\nstub\n")
    monkeypatch.setattr(solomon_llm, "_client", stub)
    return stub


# ---------------------------------------------------------------------------
# Successful end-to-end
# ---------------------------------------------------------------------------


def test_ingest_text_file_success(solomon_db, corpus_env, monkeypatch):
    envelope = {
        "summary": "policy doc",
        "entities": [{
            "slug": "acme-corp", "subtype": "customer",
            "display_name": "Acme", "aliases": ["Acme"],
            "new_info": "Acme is primary customer.",
        }],
        "concepts": [],
        "playbooks": [],
        "proposed_rules": [{
            "domain": "pricing",
            "proposed_statement": "Never quote below cost+15%",
            "verbatim_excerpt": "we never quote below cost+15%",
            "keywords": ["margin"],
            "confidence_hint": "stated",
        }],
    }
    _install_stub_llm(monkeypatch, envelope)

    src = corpus_env / "corpus" / "inbox" / "docs" / "policy.txt"
    src.write_text("Acme Corp is our customer. we never quote below cost+15%.")

    result = ig.ingest_file(src)
    assert result.status == "success"
    assert result.category == "docs"
    assert result.vector_count >= 1
    assert result.rules_written == 1
    assert len(result.wiki_pages) == 1

    # Manifest row marked success.
    row = cm.existing_for_sha(result.sha256)
    assert row["status"] == "success"
    assert row["vector_count"] == result.vector_count

    # corpus/raw/docs/<slug>.txt exists.
    raw_dir = corpus_env / "corpus" / "raw" / "docs"
    assert raw_dir.exists()
    raw_files = list(raw_dir.iterdir())
    assert len(raw_files) == 1
    assert "policy" in raw_files[0].name

    # Embeddings tagged corpus_raw and corpus_wiki.
    assert ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_RAW) >= 1
    assert ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_WIKI) >= 1

    # proposed_rules + mentoring_queue row written.
    queued = cr.list_queued()
    assert len(queued) == 1
    assert queued[0]["domain"] == "pricing"

    # Source file removed from inbox.
    assert not src.exists()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_ingest_same_file_is_idempotent(solomon_db, corpus_env, monkeypatch):
    _install_stub_llm(monkeypatch, {"entities": [], "concepts": [], "playbooks": [], "proposed_rules": []})
    src = corpus_env / "corpus" / "inbox" / "docs" / "x.txt"
    src.write_text("hello world body that is long enough to chunk and embed at least once.")
    r1 = ig.ingest_file(src)
    assert r1.status == "success"

    # Recreate the same content in inbox; same sha → skipped.
    src2 = corpus_env / "corpus" / "inbox" / "docs" / "x-again.txt"
    src2.write_text("hello world body that is long enough to chunk and embed at least once.")
    r2 = ig.ingest_file(src2)
    assert r2.status == "skipped"
    assert r2.reason == "already_ingested"


# ---------------------------------------------------------------------------
# Parking paths
# ---------------------------------------------------------------------------


def test_unsupported_extension_parks(solomon_db, corpus_env, monkeypatch):
    src = corpus_env / "corpus" / "inbox" / "docs" / "weird.xyz"
    src.write_text("???")
    r = ig.ingest_file(src)
    assert r.status == "parked"
    parked_dir = corpus_env / "corpus" / "inbox" / "_unsupported"
    assert parked_dir.exists()
    assert list(parked_dir.iterdir())


def test_oversized_file_parks(solomon_db, corpus_env, monkeypatch):
    # Force a tiny size limit via schema.md.
    schema = corpus_env / "corpus" / "schema.md"
    schema.write_text("```yaml\nlimits:\n  max_size_bytes: 10\n```\n")
    src = corpus_env / "corpus" / "inbox" / "docs" / "big.txt"
    src.write_text("x" * 1000)
    r = ig.ingest_file(src)
    assert r.status == "parked"
    assert r.reason == "oversized"
    parked = corpus_env / "corpus" / "inbox" / "_oversized"
    assert parked.exists()


# ---------------------------------------------------------------------------
# LLM-passes failure → partial
# ---------------------------------------------------------------------------


def test_llm_pass_failure_marks_partial(solomon_db, corpus_env, monkeypatch):
    # Stub that raises on the extract call.
    class _Boom:
        configured = True
        def model_for(self, tier): return "stub"
        def call(self, **kw):
            raise RuntimeError("openrouter down")
    monkeypatch.setattr(solomon_llm, "_client", _Boom())

    src = corpus_env / "corpus" / "inbox" / "docs" / "p.txt"
    src.write_text("body text for the ingest pipeline. " * 20)
    r = ig.ingest_file(src)
    assert r.status == "partial"
    assert r.reason == "llm_passes"
    assert r.vector_count >= 1  # raw embeddings were written first
    row = cm.existing_for_sha(r.sha256)
    assert row["status"] == "partial"


# ---------------------------------------------------------------------------
# Missing input
# ---------------------------------------------------------------------------


def test_missing_path_returns_failed(solomon_db):
    r = ig.ingest_file("/no/such/file")
    assert r.status == "failed"


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


def test_ingest_directory_skips_parking_subdirs(solomon_db, corpus_env, monkeypatch):
    envelope = {"entities": [], "concepts": [], "playbooks": [], "proposed_rules": []}
    _install_stub_llm(monkeypatch, envelope)

    (corpus_env / "corpus" / "inbox" / "docs" / "a.txt").write_text(
        "alpha file body content " * 10
    )
    (corpus_env / "corpus" / "inbox" / "docs" / "b.txt").write_text(
        "beta file body content " * 10
    )
    # File inside a parking subdir — must be skipped.
    parked = corpus_env / "corpus" / "inbox" / "_unsupported"
    parked.mkdir(parents=True)
    (parked / "ignored.txt").write_text("ignore me " * 10)

    results = ig.ingest_directory(corpus_env / "corpus" / "inbox")
    statuses = [r.status for r in results]
    assert statuses.count("success") == 2
    # The _unsupported one was not visited at all (no result for it).
    assert all("ignored" not in (r.raw_path or "") for r in results)

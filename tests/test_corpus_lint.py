"""Tests for solomon.corpus.lint."""

from __future__ import annotations

from pathlib import Path

import pytest

from solomon.corpus import lint as L
from solomon.corpus import embed as ce
from solomon.corpus import manifest as cm
from solomon.corpus import rules as cr
from solomon.corpus import wiki as wm
from solomon.storage.pool import cursor, execute, get_conn


def _stub_embed(monkeypatch):
    monkeypatch.setattr(
        "solomon.corpus.embed.embed_batch",
        lambda texts: [[float(len(t)), 0.1, 0.2, 0.3] for t in texts],
    )


# ---------------------------------------------------------------------------
# Orphan raw embeddings
# ---------------------------------------------------------------------------


def test_find_orphan_raw_embeddings_clean(solomon_db, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    _stub_embed(monkeypatch)
    raw_path = tmp_path / "corpus" / "raw" / "docs" / "x.txt"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text("hello")
    ce.store_section_embedding(
        source_id="raw:abc:0",
        text="hello",
        source_table=ce.SOURCE_TABLE_CORPUS_RAW,
        metadata={"raw_path": "corpus/raw/docs/x.txt"},
    )
    assert L.find_orphan_raw_embeddings() == []


def test_find_orphan_raw_embeddings_dirty(solomon_db, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    _stub_embed(monkeypatch)
    ce.store_section_embedding(
        source_id="raw:abc:0",
        text="hello",
        source_table=ce.SOURCE_TABLE_CORPUS_RAW,
        metadata={"raw_path": "corpus/raw/docs/missing.txt"},
    )
    findings = L.find_orphan_raw_embeddings()
    assert len(findings) == 1
    assert findings[0].code == "orphan_raw_embedding"
    assert findings[0].target == "raw:abc:0"


def test_find_orphan_raw_embedding_no_path(solomon_db, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    _stub_embed(monkeypatch)
    ce.store_section_embedding(
        source_id="raw:x:0",
        text="hi",
        source_table=ce.SOURCE_TABLE_CORPUS_RAW,
        metadata={},
    )
    findings = L.find_orphan_raw_embeddings()
    assert any(f.code == "orphan_raw_embedding_no_path" for f in findings)


# ---------------------------------------------------------------------------
# Wiki pages
# ---------------------------------------------------------------------------


def test_find_broken_wiki_pages(solomon_db, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    # Insert a wiki_vectors row but don't create the page file.
    wm.upsert_hashes("corpus/wiki/entities/missing.md", ["h1", "h2"])
    findings = L.find_broken_wiki_pages()
    assert len(findings) == 1
    assert findings[0].code == "broken_wiki_page"
    assert findings[0].severity == "error"


def test_find_broken_wiki_pages_clean(solomon_db, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    page = tmp_path / "corpus" / "wiki" / "entities" / "real.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: entity\n---\n## Identity\nfacts\n")
    wm.upsert_hashes("corpus/wiki/entities/real.md", ["h1"])
    assert L.find_broken_wiki_pages() == []


def test_find_orphan_wiki_embeddings(solomon_db, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    _stub_embed(monkeypatch)
    ce.store_section_embedding(
        source_id="wiki:gone:h1",
        text="x",
        source_table=ce.SOURCE_TABLE_CORPUS_WIKI,
        metadata={"wiki_path": "corpus/wiki/entities/gone.md", "slug": "gone"},
    )
    findings = L.find_orphan_wiki_embeddings()
    assert len(findings) == 1
    assert findings[0].code == "orphan_wiki_embedding"


# ---------------------------------------------------------------------------
# Forgotten files
# ---------------------------------------------------------------------------


def test_find_forgotten_with_embeddings(solomon_db, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    _stub_embed(monkeypatch)
    row_id = cm.insert_pending(
        sha="a" * 64, inbox_path="/x.txt", size_bytes=10, category="docs"
    )
    cm.mark_success(row_id, raw_path="corpus/raw/docs/x.txt", vector_count=1)
    ce.store_section_embedding(
        source_id="raw:abc:0",
        text="hello",
        source_table=ce.SOURCE_TABLE_CORPUS_RAW,
        metadata={"raw_path": "corpus/raw/docs/x.txt"},
    )
    cm.mark_forgotten(row_id)
    findings = L.find_forgotten_with_embeddings()
    assert len(findings) == 1
    assert findings[0].code == "forgotten_with_embeddings"
    assert findings[0].metadata["count"] == 1


# ---------------------------------------------------------------------------
# Orphan proposed rules
# ---------------------------------------------------------------------------


def test_find_orphan_proposed_rules(solomon_db, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    cr.write_proposed_rules(
        proposals=[{
            "domain": "pricing",
            "proposed_statement": "rule",
            "verbatim_excerpt": "we always",
            "keywords": [],
            "confidence_hint": "stated",
        }],
        source_path="corpus/raw/docs/missing.txt",
    )
    findings = L.find_orphan_proposed_rules()
    assert len(findings) == 1
    assert findings[0].code == "orphan_proposed_rule"


# ---------------------------------------------------------------------------
# run_lint + summary
# ---------------------------------------------------------------------------


def test_run_lint_collects_all_findings(solomon_db, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    _stub_embed(monkeypatch)
    # Cause two distinct findings.
    ce.store_section_embedding(
        source_id="raw:abc:0", text="x",
        source_table=ce.SOURCE_TABLE_CORPUS_RAW,
        metadata={"raw_path": "corpus/raw/missing.txt"},
    )
    wm.upsert_hashes("corpus/wiki/entities/gone.md", ["h"])
    findings = L.run_lint()
    codes = {f.code for f in findings}
    assert "orphan_raw_embedding" in codes
    assert "broken_wiki_page" in codes


def test_summary_counts(solomon_db):
    findings = [
        L.LintFinding(code="orphan_raw_embedding", severity="warn", detail=""),
        L.LintFinding(code="orphan_raw_embedding", severity="warn", detail=""),
        L.LintFinding(code="broken_wiki_page", severity="error", detail=""),
    ]
    s = L.summary(findings)
    assert s["orphan_raw_embedding"] == 2
    assert s["broken_wiki_page"] == 1
    assert s["errors"] == 1
    assert s["warnings"] == 2
    assert s["total"] == 3


def test_run_lint_clean_state(solomon_db, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    assert L.run_lint() == []

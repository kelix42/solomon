"""Tests for solomon.corpus.wiki — section split, hashing, diff cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest

from solomon.corpus import wiki as wm
from solomon.corpus import embed as ce


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_page_path_buckets(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    p = wm.page_path("entity", "acme-corp")
    assert p == tmp_path / "corpus" / "wiki" / "entities" / "acme-corp.md"
    assert wm.page_path("concept", "x").parts[-3:] == ("wiki", "concepts", "x.md")
    assert wm.page_path("playbook", "y").parts[-3:] == ("wiki", "playbooks", "y.md")


def test_page_path_unknown_type_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    with pytest.raises(ValueError):
        wm.page_path("bogus", "x")


def test_read_write_page_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    path = wm.page_path("entity", "acme")
    wm.write_page(path, "hello")
    assert wm.read_page(path) == "hello"


def test_read_page_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    assert wm.read_page(wm.page_path("entity", "nope")) == ""


# ---------------------------------------------------------------------------
# Section parsing
# ---------------------------------------------------------------------------


def test_split_sections_preface_and_headings():
    content = "---\nfront: matter\n---\nintro\n\n## A\nbody a\n\n## B\nbody b\n"
    out = wm.split_sections(content)
    headers = [h for h, _ in out]
    assert headers[0] == "__preface__"
    assert "## A" in headers
    assert "## B" in headers


def test_split_sections_empty():
    assert wm.split_sections("") == []
    assert wm.split_sections("   \n   ") == []


def test_section_hash_deterministic():
    a = wm.section_hash("## A", "body")
    b = wm.section_hash("## A", "body")
    c = wm.section_hash("## A", "different body")
    assert a == b
    assert a != c


def test_hashes_for_page_returns_four_tuples():
    content = "## A\nbody\n## B\nmore body\n"
    out = wm.hashes_for_page(content)
    assert len(out) == 2
    for h, header, body, full in out:
        assert isinstance(h, str) and len(h) == 64
        assert header.startswith("## ")
        assert full.startswith("## ")


# ---------------------------------------------------------------------------
# wiki_vectors persistence
# ---------------------------------------------------------------------------


def test_previous_hashes_empty(solomon_db):
    assert wm.previous_hashes("corpus/wiki/entities/x.md") == []


def test_upsert_hashes_roundtrip(solomon_db):
    wm.upsert_hashes("p.md", ["a", "b", "c"])
    assert wm.previous_hashes("p.md") == ["a", "b", "c"]
    # Re-upsert replaces.
    wm.upsert_hashes("p.md", ["d"])
    assert wm.previous_hashes("p.md") == ["d"]


def test_delete_page_hashes(solomon_db):
    wm.upsert_hashes("p.md", ["a"])
    wm.delete_page_hashes("p.md")
    assert wm.previous_hashes("p.md") == []


# ---------------------------------------------------------------------------
# embed_and_upsert_page — the diff core
# ---------------------------------------------------------------------------


def _stub_embed_batch(monkeypatch):
    """Deterministic stub: each input is a 4-d vector of (len, 0.1, 0.2, 0.3)."""
    monkeypatch.setattr(
        "solomon.corpus.embed.embed_batch",
        lambda texts: [[float(len(t)), 0.1, 0.2, 0.3] for t in texts],
    )


def test_embed_first_time_writes_all_sections(solomon_db, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    _stub_embed_batch(monkeypatch)
    content = "---\ntype: entity\n---\nintro\n\n## Identity\nfacts\n\n## Sources\n- x\n"
    written = wm.embed_and_upsert_page(
        page_type="entity",
        slug="acme",
        page_path_str="corpus/wiki/entities/acme.md",
        new_content=content,
    )
    # preface + 2 sections = 3 vectors
    assert written == 3
    assert ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_WIKI) == 3
    hashes = wm.previous_hashes("corpus/wiki/entities/acme.md")
    assert len(hashes) == 3


def test_embed_idempotent_no_changes(solomon_db, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    _stub_embed_batch(monkeypatch)
    content = "## A\nbody\n## B\nmore\n"
    w1 = wm.embed_and_upsert_page(
        page_type="entity", slug="x", page_path_str="p.md", new_content=content
    )
    assert w1 == 2
    # Same content → no new writes.
    w2 = wm.embed_and_upsert_page(
        page_type="entity", slug="x", page_path_str="p.md", new_content=content
    )
    assert w2 == 0
    assert ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_WIKI) == 2


def test_embed_deletes_orphans_on_section_drop(solomon_db, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    _stub_embed_batch(monkeypatch)
    v1 = "## A\nbody a\n## B\nbody b\n## C\nbody c\n"
    wm.embed_and_upsert_page(
        page_type="entity", slug="x", page_path_str="p.md", new_content=v1
    )
    assert ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_WIKI) == 3

    # Drop section B; section A's text changes; section C unchanged.
    v2 = "## A\nbody a edited\n## C\nbody c\n"
    written = wm.embed_and_upsert_page(
        page_type="entity", slug="x", page_path_str="p.md", new_content=v2
    )
    # A is "new" (hash changed); B is gone (deleted); C unchanged.
    # Net new writes: 1 (the new A).
    # Net deletions: 2 (the old A + B).
    assert written == 1
    # 3 - 2 deleted + 1 new = 2 total
    assert ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_WIKI) == 2


def test_remove_page_cascades(solomon_db, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    _stub_embed_batch(monkeypatch)
    v = "## A\nbody\n## B\nmore\n"
    wm.embed_and_upsert_page(
        page_type="entity", slug="x", page_path_str="p.md", new_content=v
    )
    # Also write a real file so the unlink path executes.
    wm.write_page(wm.page_path("entity", "x"), v)
    assert wm.page_path("entity", "x").exists()

    removed = wm.remove_page(
        page_type="entity", slug="x", page_path_str="p.md"
    )
    assert removed == 2
    assert wm.previous_hashes("p.md") == []
    assert not wm.page_path("entity", "x").exists()
    assert ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_WIKI) == 0


def test_embed_skips_empty_preface(solomon_db, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    _stub_embed_batch(monkeypatch)
    # No preface text at all.
    content = "## A\nbody a\n## B\nbody b\n"
    written = wm.embed_and_upsert_page(
        page_type="entity", slug="z", page_path_str="p.md", new_content=content
    )
    assert written == 2  # not 3 — no preface

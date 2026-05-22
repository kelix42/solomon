"""Tests for solomon.corpus.forget."""

from __future__ import annotations

from pathlib import Path

import pytest

from solomon.corpus import embed as ce
from solomon.corpus import forget as fg
from solomon.corpus import manifest as cm
from solomon.corpus import rules as cr
from solomon.storage.pool import cursor, execute, get_conn


def _stub_embed(monkeypatch):
    monkeypatch.setattr(
        "solomon.corpus.embed.embed_batch",
        lambda texts: [[float(len(t)), 0.1, 0.2, 0.3] for t in texts],
    )


def _seed_ingested_file(tmp_path, monkeypatch, *, sha, rel_raw="corpus/raw/docs/x.txt"):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    raw = tmp_path / rel_raw
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("body")
    row_id = cm.insert_pending(
        sha=sha, inbox_path="/tmp/x.txt", size_bytes=4, category="docs"
    )
    cm.mark_success(row_id, raw_path=rel_raw, vector_count=1)
    return row_id, rel_raw, raw


def test_forget_missing_file_returns_not_found(solomon_db):
    s = fg.forget_file(sha256="not-a-real-hash")
    assert s["found"] is False


def test_forget_by_sha_cascades(solomon_db, monkeypatch, tmp_path):
    _stub_embed(monkeypatch)
    row_id, rel, raw = _seed_ingested_file(tmp_path, monkeypatch, sha="a" * 64)
    ce.store_section_embedding(
        source_id="raw:abc:0",
        text="hello",
        source_table=ce.SOURCE_TABLE_CORPUS_RAW,
        metadata={"raw_path": rel},
    )
    cr.write_proposed_rules(
        proposals=[{
            "domain": "pricing",
            "proposed_statement": "rule",
            "verbatim_excerpt": "we always",
            "keywords": [],
            "confidence_hint": "stated",
        }],
        source_path=rel,
    )

    assert raw.exists()
    assert ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_RAW) == 1
    assert len(cr.list_queued()) == 1

    summary = fg.forget_file(sha256="a" * 64)
    assert summary["found"] is True
    assert summary["disk_deleted"] is True
    assert summary["embeddings_deleted"] == 1
    assert summary["rules_deleted"] == 1
    assert not raw.exists()
    assert ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_RAW) == 0
    assert cr.list_queued() == []

    # ingested_files row marked forgotten.
    row = cm.existing_for_sha("a" * 64)
    assert row["status"] == "forgotten"


def test_forget_by_file_id(solomon_db, monkeypatch, tmp_path):
    _stub_embed(monkeypatch)
    row_id, rel, raw = _seed_ingested_file(tmp_path, monkeypatch, sha="b" * 64)
    summary = fg.forget_file(file_id=row_id)
    assert summary["found"] is True
    assert summary["file_id"] == row_id
    assert summary["disk_deleted"] is True


def test_forget_by_raw_path(solomon_db, monkeypatch, tmp_path):
    _stub_embed(monkeypatch)
    row_id, rel, raw = _seed_ingested_file(tmp_path, monkeypatch, sha="c" * 64)
    summary = fg.forget_file(raw_path=rel)
    assert summary["found"] is True
    assert summary["raw_path"] == rel


def test_forget_idempotent(solomon_db, monkeypatch, tmp_path):
    _stub_embed(monkeypatch)
    row_id, rel, raw = _seed_ingested_file(tmp_path, monkeypatch, sha="d" * 64)
    fg.forget_file(sha256="d" * 64)
    # Second call: file already gone, row already forgotten — should not blow up.
    summary = fg.forget_file(sha256="d" * 64)
    assert summary["found"] is True
    assert summary["disk_deleted"] is False  # file already deleted
    assert summary["embeddings_deleted"] == 0


def test_forget_keeps_other_files_intact(solomon_db, monkeypatch, tmp_path):
    _stub_embed(monkeypatch)
    _, rel1, raw1 = _seed_ingested_file(tmp_path, monkeypatch, sha="e" * 64,
                                        rel_raw="corpus/raw/docs/a.txt")
    _, rel2, raw2 = _seed_ingested_file(tmp_path, monkeypatch, sha="f" * 64,
                                        rel_raw="corpus/raw/docs/b.txt")
    ce.store_section_embedding(
        source_id="raw:a:0", text="hello",
        source_table=ce.SOURCE_TABLE_CORPUS_RAW,
        metadata={"raw_path": rel1},
    )
    ce.store_section_embedding(
        source_id="raw:b:0", text="world",
        source_table=ce.SOURCE_TABLE_CORPUS_RAW,
        metadata={"raw_path": rel2},
    )
    fg.forget_file(sha256="e" * 64)
    assert raw1.exists() is False
    assert raw2.exists()
    assert ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_RAW) == 1
    remaining = ce.list_source_ids(ce.SOURCE_TABLE_CORPUS_RAW)
    assert remaining == ["raw:b:0"]


def test_forget_can_skip_disk_delete(solomon_db, monkeypatch, tmp_path):
    _stub_embed(monkeypatch)
    row_id, rel, raw = _seed_ingested_file(tmp_path, monkeypatch, sha="0" * 64)
    summary = fg.forget_file(sha256="0" * 64, delete_disk=False)
    assert summary["found"] is True
    assert summary["disk_deleted"] is False
    assert raw.exists()  # file preserved

"""Tests for solomon.sleep.job_9_corpus_lint."""

from __future__ import annotations

import json

import pytest

from solomon.sleep import job_9_corpus_lint as J
from solomon.storage.pool import cursor, execute, get_conn, parse_json


def _seed_broken_wiki(tmp_path, monkeypatch):
    """Create a wiki_vectors row pointing at a missing page file."""
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    # The corpus dir is intentionally missing the wiki page — that's the
    # error condition find_broken_wiki_pages catches (severity='error').
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO wiki_vectors (page_path, tenant_id, section_hashes) "
                "VALUES (?, ?, ?)",
                ("corpus/wiki/missing-page.md", "default", "{}"),
            )
        conn.commit()


def _count_lint_queue(tenant_id: str = "default") -> int:
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT COUNT(*) FROM mentoring_queue "
                "WHERE tenant_id=? AND source='lint_finding'",
                (tenant_id,),
            )
            return int(cur.fetchone()[0])


def test_job_9_enqueues_error_findings(solomon_db, tmp_path, monkeypatch):
    _seed_broken_wiki(tmp_path, monkeypatch)
    assert _count_lint_queue() == 0

    result = J.run(tenant_id="default")

    assert _count_lint_queue() == 1
    assert result["enqueued"] == 1
    assert result["errors_seen"] >= 1

    # Spot-check the enqueued row.
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT source, priority, payload FROM mentoring_queue "
                "WHERE tenant_id=? AND source='lint_finding'",
                ("default",),
            )
            row = cur.fetchone()
    assert row[0] == "lint_finding"
    assert row[1] == 2
    payload = parse_json(row[2])
    assert payload["code"] == "broken_wiki_page"
    assert payload["severity"] == "error"
    assert "missing-page.md" in payload["target"]


def test_job_9_skips_warnings(solomon_db, tmp_path, monkeypatch):
    """find_orphan_raw_embeddings returns severity='warn'; should NOT queue."""
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    # Stub embed_batch so store_section_embedding doesn't try to load a model.
    monkeypatch.setattr(
        "solomon.corpus.embed.embed_batch",
        lambda texts: [[float(len(t)), 0.1, 0.2, 0.3] for t in texts],
    )
    from solomon.corpus import embed as ce
    ce.store_section_embedding(
        source_id="raw:abc:0",
        text="hello",
        source_table=ce.SOURCE_TABLE_CORPUS_RAW,
        metadata={"raw_path": "corpus/raw/docs/missing.txt"},  # file doesn't exist
    )

    result = J.run(tenant_id="default")

    # The orphan_raw_embedding is severity='warn' — must not enqueue.
    assert _count_lint_queue() == 0
    assert result["enqueued"] == 0
    assert result["warnings_seen"] >= 1


def test_job_9_is_idempotent(solomon_db, tmp_path, monkeypatch):
    _seed_broken_wiki(tmp_path, monkeypatch)

    J.run(tenant_id="default")
    J.run(tenant_id="default")
    J.run(tenant_id="default")

    assert _count_lint_queue() == 1


def test_job_9_empty_when_corpus_clean(solomon_db, tmp_path, monkeypatch):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    result = J.run(tenant_id="default")
    assert result["enqueued"] == 0
    assert _count_lint_queue() == 0

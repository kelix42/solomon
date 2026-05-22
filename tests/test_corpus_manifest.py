"""Tests for solomon.corpus.manifest."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from solomon.corpus import manifest as m


def test_file_sha256_matches_hashlib(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello world")
    assert m.file_sha256(p) == hashlib.sha256(b"hello world").hexdigest()


def test_file_sha256_streams_large_files(tmp_path):
    p = tmp_path / "big.bin"
    blob = b"x" * (1 << 20)  # 1 MiB
    p.write_bytes(blob)
    assert m.file_sha256(p, chunk_size=4096) == hashlib.sha256(blob).hexdigest()


def test_insert_pending_creates_row(solomon_db):
    row_id = m.insert_pending(
        sha="a" * 64, inbox_path="/x/y.txt", size_bytes=42, category="docs"
    )
    assert isinstance(row_id, str) and len(row_id) == 32
    row = m.existing_for_sha("a" * 64)
    assert row is not None
    assert row["status"] == "pending"
    assert row["category"] == "docs"
    assert row["size_bytes"] == 42


def test_insert_pending_idempotent_on_sha(solomon_db):
    """Two inserts with the same SHA return the same row id."""
    id1 = m.insert_pending(sha="b" * 64, inbox_path="/x.txt", size_bytes=10, category="docs")
    id2 = m.insert_pending(sha="b" * 64, inbox_path="/x.txt", size_bytes=10, category="docs")
    assert id1 == id2


def test_state_transitions(solomon_db):
    row_id = m.insert_pending(
        sha="c" * 64, inbox_path="/x.txt", size_bytes=10, category="docs"
    )
    m.mark_in_progress(row_id)
    assert m.existing_for_sha("c" * 64)["status"] == "in_progress"

    m.mark_success(row_id, raw_path="corpus/raw/docs/foo.txt", vector_count=7,
                   wiki_pages_touched=["corpus/wiki/entities/acme.md"])
    row = m.existing_for_sha("c" * 64)
    assert row["status"] == "success"
    assert row["vector_count"] == 7
    assert row["raw_path"] == "corpus/raw/docs/foo.txt"
    assert row["wiki_pages_touched"] == ["corpus/wiki/entities/acme.md"]


def test_mark_partial_carries_error(solomon_db):
    row_id = m.insert_pending(
        sha="d" * 64, inbox_path="/x.txt", size_bytes=10, category="docs"
    )
    m.mark_partial(row_id, raw_path="raw/x.txt", vector_count=2, error="pinecone timeout")
    row = m.existing_for_sha("d" * 64)
    assert row["status"] == "partial"
    assert row["error_message"] == "pinecone timeout"


def test_mark_failed(solomon_db):
    row_id = m.insert_pending(
        sha="e" * 64, inbox_path="/x.txt", size_bytes=10, category="docs"
    )
    m.mark_failed(row_id, "bad parse")
    row = m.existing_for_sha("e" * 64)
    assert row["status"] == "failed"
    assert row["error_message"] == "bad parse"


def test_is_already_ingested_only_when_success(solomon_db):
    sha = "f" * 64
    row_id = m.insert_pending(
        sha=sha, inbox_path="/x.txt", size_bytes=10, category="docs"
    )
    assert m.is_already_ingested(sha) is False
    m.mark_in_progress(row_id)
    assert m.is_already_ingested(sha) is False
    m.mark_success(row_id, raw_path="raw/x.txt", vector_count=0)
    assert m.is_already_ingested(sha) is True


def test_existing_for_sha_missing_returns_none(solomon_db):
    assert m.existing_for_sha("0" * 64) is None


def test_list_by_status(solomon_db):
    m.insert_pending(sha="1" * 64, inbox_path="/a", size_bytes=1, category="docs")
    rid2 = m.insert_pending(sha="2" * 64, inbox_path="/b", size_bytes=1, category="docs")
    m.mark_success(rid2, raw_path="raw/b", vector_count=3)
    pending = m.list_by_status("pending")
    success = m.list_by_status("success")
    assert {r["sha256"] for r in pending} == {"1" * 64}
    assert {r["sha256"] for r in success} == {"2" * 64}


def test_list_by_status_invalid_raises(solomon_db):
    with pytest.raises(ValueError):
        m.list_by_status("bogus")


def test_stats_counts(solomon_db):
    m.insert_pending(sha="1" * 64, inbox_path="/a", size_bytes=1, category="docs")
    rid2 = m.insert_pending(sha="2" * 64, inbox_path="/b", size_bytes=1, category="docs")
    rid3 = m.insert_pending(sha="3" * 64, inbox_path="/c", size_bytes=1, category="docs")
    m.mark_success(rid2, raw_path="raw/b", vector_count=0)
    m.mark_failed(rid3, "boom")
    s = m.stats()
    assert s["pending"] == 1
    assert s["success"] == 1
    assert s["failed"] == 1
    assert s["total"] == 3

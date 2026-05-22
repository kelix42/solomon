"""Tests for solomon.corpus.embed.

We monkey-patch ``solomon.ingestion.embedder.embed_batch`` with a deterministic
stub so the tests run without downloading a model or hitting an API. The
real backend is exercised end-to-end in the orchestrator integration test.
"""

from __future__ import annotations

import struct

import pytest

from solomon.corpus import embed as ce
from solomon.corpus.chunk import Chunk


def _stub_embed_batch(monkeypatch, returner=None):
    """Replace embed_batch with a deterministic stub.

    Default behaviour: each input text becomes a 4-d vector of (len(text), 0.1, 0.2, 0.3).
    Passing ``returner`` overrides per-call output.
    """
    calls = []

    def stub(texts):
        calls.append(list(texts))
        if returner is not None:
            return returner(texts)
        return [[float(len(t)), 0.1, 0.2, 0.3] for t in texts]

    monkeypatch.setattr("solomon.corpus.embed.embed_batch", stub)
    return calls


# ---------------------------------------------------------------------------
# encode / decode
# ---------------------------------------------------------------------------


def test_encode_decode_roundtrip(solomon_db):
    vec = [1.0, 2.0, 3.0, 4.5]
    encoded = ce.encode_vector(vec)
    # SQLite path packs to bytes.
    assert isinstance(encoded, (bytes, bytearray))
    assert ce.decode_vector(encoded) == pytest.approx(vec)


def test_decode_handles_none():
    assert ce.decode_vector(None) == []


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_invalid_source_table_raises(solomon_db):
    with pytest.raises(ValueError):
        ce.store_section_embedding(source_id="x", text="t", source_table="bogus")


# ---------------------------------------------------------------------------
# store + read paths
# ---------------------------------------------------------------------------


def test_store_section_embedding_inserts_row(solomon_db, monkeypatch):
    _stub_embed_batch(monkeypatch)
    eid = ce.store_section_embedding(
        source_id="wiki:acme:hash1",
        text="hello",
        source_table=ce.SOURCE_TABLE_CORPUS_WIKI,
        metadata={"slug": "acme"},
    )
    assert isinstance(eid, int)
    row = ce.get_row(source_table=ce.SOURCE_TABLE_CORPUS_WIKI, source_id="wiki:acme:hash1")
    assert row is not None
    assert row["metadata"] == {"slug": "acme"}
    # vector should round-trip — first dim is len("hello")=5.
    assert row["vector"][0] == pytest.approx(5.0)


def test_store_section_skips_empty(solomon_db, monkeypatch):
    _stub_embed_batch(monkeypatch)
    assert ce.store_section_embedding(
        source_id="x", text="   ", source_table=ce.SOURCE_TABLE_CORPUS_WIKI
    ) is None


def test_store_section_handles_failed_embedding(solomon_db, monkeypatch):
    _stub_embed_batch(monkeypatch, returner=lambda texts: [None] * len(texts))
    assert ce.store_section_embedding(
        source_id="x", text="hello", source_table=ce.SOURCE_TABLE_CORPUS_WIKI
    ) is None


def test_store_section_upsert_replaces_existing(solomon_db, monkeypatch):
    _stub_embed_batch(monkeypatch)
    eid1 = ce.store_section_embedding(
        source_id="wiki:x:h", text="first", source_table=ce.SOURCE_TABLE_CORPUS_WIKI,
        metadata={"v": 1},
    )
    eid2 = ce.store_section_embedding(
        source_id="wiki:x:h", text="second", source_table=ce.SOURCE_TABLE_CORPUS_WIKI,
        metadata={"v": 2},
    )
    assert eid1 != eid2  # delete-then-insert means a new id
    row = ce.get_row(source_table=ce.SOURCE_TABLE_CORPUS_WIKI, source_id="wiki:x:h")
    assert row["metadata"] == {"v": 2}
    assert ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_WIKI) == 1


def test_store_chunk_embeddings_inserts_n_rows(solomon_db, monkeypatch):
    _stub_embed_batch(monkeypatch)
    chunks = [
        Chunk(seq=0, text="alpha", char_offsets=(0, 5), source_section="intro"),
        Chunk(seq=1, text="beta",  char_offsets=(5, 9), source_section="body"),
        Chunk(seq=2, text="gamma", char_offsets=(9, 14)),
    ]
    ids = ce.store_chunk_embeddings(
        source_id_prefix="raw:abc12345",
        chunks=chunks,
        source_table=ce.SOURCE_TABLE_CORPUS_RAW,
        extra_metadata={"category": "docs"},
    )
    assert len(ids) == 3
    assert ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_RAW) == 3
    # Each row carries the prefix + seq.
    sids = ce.list_source_ids(ce.SOURCE_TABLE_CORPUS_RAW, prefix="raw:abc12345")
    assert set(sids) == {"raw:abc12345:0", "raw:abc12345:1", "raw:abc12345:2"}
    row = ce.get_row(source_table=ce.SOURCE_TABLE_CORPUS_RAW, source_id="raw:abc12345:1")
    assert row["metadata"]["source_section"] == "body"
    assert row["metadata"]["category"] == "docs"
    assert row["metadata"]["seq"] == 1
    assert row["metadata"]["char_offsets"] == [5, 9]


def test_store_chunk_skips_failed_embedding(solomon_db, monkeypatch):
    def returner(texts):
        # Fail the second one.
        return [[0.1] * 4, None, [0.3] * 4]
    _stub_embed_batch(monkeypatch, returner=returner)
    chunks = [
        Chunk(seq=0, text="a", char_offsets=(0, 1)),
        Chunk(seq=1, text="b", char_offsets=(1, 2)),
        Chunk(seq=2, text="c", char_offsets=(2, 3)),
    ]
    ids = ce.store_chunk_embeddings(
        source_id_prefix="raw:x", chunks=chunks,
        source_table=ce.SOURCE_TABLE_CORPUS_RAW,
    )
    assert len(ids) == 2
    assert ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_RAW) == 2


def test_delete_by_source_ids(solomon_db, monkeypatch):
    _stub_embed_batch(monkeypatch)
    chunks = [Chunk(seq=i, text=f"t{i}", char_offsets=(0, 1)) for i in range(5)]
    ce.store_chunk_embeddings(
        source_id_prefix="raw:zz", chunks=chunks,
        source_table=ce.SOURCE_TABLE_CORPUS_RAW,
    )
    assert ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_RAW) == 5
    n = ce.delete_by_source_ids(
        ce.SOURCE_TABLE_CORPUS_RAW,
        ["raw:zz:1", "raw:zz:3", "raw:zz:does_not_exist"],
    )
    assert n == 2
    assert ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_RAW) == 3


def test_list_source_ids_prefix_filter(solomon_db, monkeypatch):
    _stub_embed_batch(monkeypatch)
    ce.store_section_embedding(source_id="raw:abc:0", text="x",
                               source_table=ce.SOURCE_TABLE_CORPUS_RAW)
    ce.store_section_embedding(source_id="raw:xyz:0", text="y",
                               source_table=ce.SOURCE_TABLE_CORPUS_RAW)
    ce.store_section_embedding(source_id="wiki:foo:h", text="z",
                               source_table=ce.SOURCE_TABLE_CORPUS_WIKI)
    raw_abc = ce.list_source_ids(ce.SOURCE_TABLE_CORPUS_RAW, prefix="raw:abc")
    assert raw_abc == ["raw:abc:0"]
    # source_table partitioning is enforced.
    assert ce.list_source_ids(ce.SOURCE_TABLE_CORPUS_WIKI) == ["wiki:foo:h"]


def test_empty_chunks_returns_empty(solomon_db, monkeypatch):
    _stub_embed_batch(monkeypatch)
    out = ce.store_chunk_embeddings(
        source_id_prefix="raw:none", chunks=[],
        source_table=ce.SOURCE_TABLE_CORPUS_RAW,
    )
    assert out == []

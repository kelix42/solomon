"""Persist embeddings to the unified ``embeddings`` table.

REPORT-CORPUS.md §3 and §4.3: Drive's four Pinecone namespaces collapse
into a ``source_table`` discriminator on a single table. This module is
the only place corpus code talks to the embeddings store; it adds the
``corpus_raw`` / ``corpus_wiki`` discriminator and wraps the existing
``solomon.ingestion.embedder.embed_batch`` for vector generation.

We store vectors as packed float32 BLOBs on SQLite (matches schema.sql)
and as pgvector ``vector(N)`` on Postgres (psycopg adapts a Python list
of floats automatically).

Public surface:
  - ``store_chunk_embeddings(*, source_id_prefix, chunks, source_table)``
        Embed N chunks in one batch, insert N rows. Returns the list of
        inserted embedding_ids.
  - ``store_section_embedding(*, source_id, text, source_table, metadata)``
        Single-row variant used by wiki.py for per-section vectors.
  - ``delete_by_source_ids(source_table, source_ids)`` — orphan cleanup.
  - ``list_source_ids(source_table, *, prefix=None, tenant_id=None)`` —
        introspection for lint.py.

Source-ID conventions (string column on the embeddings table):
  - corpus_raw   : ``raw:<sha8>:<chunk_idx>``
  - corpus_wiki  : ``wiki:<slug>:<section_hash>``
  - decisions    : ``decision:<decision_id>``  (handled elsewhere)
  - captured_items: ``captured:<captured_id>`` (handled elsewhere)
"""

from __future__ import annotations

import logging
import os
import struct
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..ingestion.embedder import dimension, embed_batch
from ..storage.pool import (
    backend,
    cursor,
    execute,
    executemany,
    get_conn,
    jsonify,
    parse_json,
    row_to_dict,
)

logger = logging.getLogger("solomon.corpus.embed")

# Valid source_table values per schema.sql + REPORT-CORPUS.md §4.3.
SOURCE_TABLE_CORPUS_RAW = "corpus_raw"
SOURCE_TABLE_CORPUS_WIKI = "corpus_wiki"
SOURCE_TABLE_CAPTURED = "captured_items"
SOURCE_TABLE_DECISIONS = "decisions"

VALID_SOURCE_TABLES = {
    SOURCE_TABLE_CORPUS_RAW,
    SOURCE_TABLE_CORPUS_WIKI,
    SOURCE_TABLE_CAPTURED,
    SOURCE_TABLE_DECISIONS,
}


# ---------------------------------------------------------------------------
# Vector encoding
# ---------------------------------------------------------------------------


def encode_vector(vec: Sequence[float]) -> Any:
    """Convert a list of floats to whatever the active backend stores.

    SQLite: packed little-endian float32 BLOB (matches schema.sql).
    Postgres: leaves the list as-is so psycopg + pgvector adapt it.
    """
    if backend() == "postgres":
        return list(vec)
    return struct.pack(f"<{len(vec)}f", *vec)


def decode_vector(blob: Any) -> List[float]:
    """Inverse of encode_vector. SQLite gives bytes; Postgres gives a list."""
    if blob is None:
        return []
    if isinstance(blob, (list, tuple)):
        return list(blob)
    if isinstance(blob, (bytes, bytearray, memoryview)):
        b = bytes(blob)
        n = len(b) // 4
        return list(struct.unpack(f"<{n}f", b))
    return []


def _default_tenant() -> str:
    return os.getenv("SOLOMON_TENANT_ID", "default")


def _validate_source_table(t: str) -> None:
    if t not in VALID_SOURCE_TABLES:
        raise ValueError(
            f"source_table {t!r} not in {sorted(VALID_SOURCE_TABLES)}"
        )


# ---------------------------------------------------------------------------
# Inserts
# ---------------------------------------------------------------------------


def store_section_embedding(
    *,
    source_id: str,
    text: str,
    source_table: str,
    metadata: Optional[Dict[str, Any]] = None,
    tenant_id: Optional[str] = None,
) -> Optional[int]:
    """Embed one piece of text + insert one row. Returns embedding_id.

    On dupe (UNIQUE on (tenant_id, source_table, source_id)) we replace.
    """
    _validate_source_table(source_table)
    if not text or not text.strip():
        return None
    vec = embed_batch([text])[0]
    if vec is None:
        return None
    tid = tenant_id or _default_tenant()
    return _upsert_one(
        tenant_id=tid,
        source_table=source_table,
        source_id=source_id,
        vector=vec,
        metadata=metadata or {},
    )


def store_chunk_embeddings(
    *,
    source_id_prefix: str,
    chunks: List[Any],  # iterable of objects with .text and .seq (corpus.chunk.Chunk)
    source_table: str,
    extra_metadata: Optional[Dict[str, Any]] = None,
    tenant_id: Optional[str] = None,
) -> List[int]:
    """Batch-embed a list of Chunks and insert them.

    Source IDs are generated as ``{source_id_prefix}:{chunk.seq}`` which is
    deterministic — re-embedding the same document overwrites the same
    rows via the UNIQUE constraint.
    """
    _validate_source_table(source_table)
    if not chunks:
        return []
    texts = [getattr(c, "text", "") for c in chunks]
    vectors = embed_batch(texts)
    tid = tenant_id or _default_tenant()
    out: List[int] = []
    for ch, vec in zip(chunks, vectors):
        if vec is None:
            continue
        meta: Dict[str, Any] = dict(extra_metadata or {})
        meta["seq"] = getattr(ch, "seq", None)
        if getattr(ch, "source_section", None):
            meta["source_section"] = ch.source_section
        if getattr(ch, "char_offsets", None):
            meta["char_offsets"] = list(ch.char_offsets)
        # Truncate text snippet for citation use.
        meta.setdefault("text", (getattr(ch, "text", "") or "")[:1000])
        eid = _upsert_one(
            tenant_id=tid,
            source_table=source_table,
            source_id=f"{source_id_prefix}:{getattr(ch, 'seq', 0)}",
            vector=vec,
            metadata=meta,
        )
        if eid is not None:
            out.append(eid)
    return out


def _upsert_one(
    *,
    tenant_id: str,
    source_table: str,
    source_id: str,
    vector: List[float],
    metadata: Dict[str, Any],
) -> Optional[int]:
    encoded = encode_vector(vector)
    md_json = jsonify(metadata)
    with get_conn() as conn:
        with cursor(conn) as cur:
            # Delete any existing row first (the UNIQUE-aware INSERT … ON
            # CONFLICT … syntax differs between SQLite + Postgres; doing
            # delete-then-insert keeps the helper portable).
            execute(
                cur,
                "DELETE FROM embeddings "
                "WHERE tenant_id = ? AND source_table = ? AND source_id = ?",
                (tenant_id, source_table, source_id),
            )
            execute(
                cur,
                "INSERT INTO embeddings "
                "(tenant_id, source_table, source_id, vector, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (tenant_id, source_table, source_id, encoded, md_json),
            )
            eid: Optional[int] = getattr(cur, "lastrowid", None)
            if eid is None:
                # Postgres path — fetch the just-inserted row.
                execute(
                    cur,
                    "SELECT embedding_id FROM embeddings "
                    "WHERE tenant_id = ? AND source_table = ? AND source_id = ?",
                    (tenant_id, source_table, source_id),
                )
                row = cur.fetchone()
                eid = int(row[0]) if row else None
        conn.commit()
    return eid


# ---------------------------------------------------------------------------
# Reads / deletes
# ---------------------------------------------------------------------------


def list_source_ids(
    source_table: str,
    *,
    prefix: Optional[str] = None,
    tenant_id: Optional[str] = None,
    limit: int = 10000,
) -> List[str]:
    _validate_source_table(source_table)
    tid = tenant_id or _default_tenant()
    out: List[str] = []
    with get_conn() as conn:
        with cursor(conn) as cur:
            if prefix:
                execute(
                    cur,
                    "SELECT source_id FROM embeddings "
                    "WHERE tenant_id = ? AND source_table = ? AND source_id LIKE ? "
                    "ORDER BY embedding_id ASC LIMIT ?",
                    (tid, source_table, f"{prefix}%", limit),
                )
            else:
                execute(
                    cur,
                    "SELECT source_id FROM embeddings "
                    "WHERE tenant_id = ? AND source_table = ? "
                    "ORDER BY embedding_id ASC LIMIT ?",
                    (tid, source_table, limit),
                )
            for r in cur.fetchall():
                out.append(r[0])
    return out


def delete_by_source_ids(
    source_table: str,
    source_ids: Iterable[str],
    *,
    tenant_id: Optional[str] = None,
) -> int:
    """Delete rows by exact source_id match. Returns count deleted."""
    _validate_source_table(source_table)
    ids = [s for s in source_ids if s]
    if not ids:
        return 0
    tid = tenant_id or _default_tenant()
    deleted = 0
    with get_conn() as conn:
        with cursor(conn) as cur:
            for sid in ids:
                execute(
                    cur,
                    "DELETE FROM embeddings "
                    "WHERE tenant_id = ? AND source_table = ? AND source_id = ?",
                    (tid, source_table, sid),
                )
                deleted += int(getattr(cur, "rowcount", 0) or 0)
        conn.commit()
    return deleted


def count_for_source_table(
    source_table: str,
    *,
    tenant_id: Optional[str] = None,
) -> int:
    _validate_source_table(source_table)
    tid = tenant_id or _default_tenant()
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT COUNT(*) FROM embeddings "
                "WHERE tenant_id = ? AND source_table = ?",
                (tid, source_table),
            )
            row = cur.fetchone()
    return int(row[0]) if row else 0


def get_row(
    *,
    source_table: str,
    source_id: str,
    tenant_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return the one row matching (source_table, source_id) or None.

    metadata is JSON-decoded; the vector is decoded into a Python list.
    """
    _validate_source_table(source_table)
    tid = tenant_id or _default_tenant()
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT embedding_id, tenant_id, source_table, source_id, "
                "       vector, metadata "
                "FROM embeddings "
                "WHERE tenant_id = ? AND source_table = ? AND source_id = ?",
                (tid, source_table, source_id),
            )
            row = cur.fetchone()
    if not row:
        return None
    keys = ["embedding_id", "tenant_id", "source_table", "source_id", "vector", "metadata"]
    if hasattr(row, "keys"):
        d = {k: row[k] for k in keys if k in row.keys()}
    else:
        d = dict(zip(keys, row))
    d["vector"] = decode_vector(d.get("vector"))
    d["metadata"] = parse_json(d.get("metadata")) or {}
    return d


# ---------------------------------------------------------------------------
# Re-exports for convenience
# ---------------------------------------------------------------------------

__all__ = [
    "SOURCE_TABLE_CORPUS_RAW",
    "SOURCE_TABLE_CORPUS_WIKI",
    "SOURCE_TABLE_CAPTURED",
    "SOURCE_TABLE_DECISIONS",
    "VALID_SOURCE_TABLES",
    "count_for_source_table",
    "decode_vector",
    "delete_by_source_ids",
    "dimension",
    "encode_vector",
    "get_row",
    "list_source_ids",
    "store_chunk_embeddings",
    "store_section_embedding",
]

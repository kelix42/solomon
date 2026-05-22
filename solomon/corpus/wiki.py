"""Wiki page I/O + section-hash diff against the embeddings table.

REPORT-CORPUS.md §1.4 + §4.5 Phase B. Wiki vector convention:

  * Each ``## Heading`` section of a wiki page becomes ONE row in the
    embeddings table with ``source_table='corpus_wiki'`` and
    ``source_id = wiki:<slug>:<section_hash>``.
  * The ``wiki_vectors`` table stores the live section_hashes per page
    as a JSON list. On re-embed we diff old vs new hashes, delete the
    embeddings rows for gone sections, and embed only the new ones.
    Idempotent: a re-write that doesn't change any section produces zero
    new vectors.

Ported from /root/projects/solomon-from-drive/corpus_ingest/wiki.py with
two substitutions:

  - Pinecone upsert / delete → ``solomon.corpus.embed`` against the unified
    embeddings table.
  - Raw sqlite3 → ``solomon.storage.pool`` (``?`` placeholders).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from . import embed as corpus_embed
from .schema_config import corpus_root
from ..storage.pool import cursor, execute, get_conn, jsonify, parse_json

logger = logging.getLogger("solomon.corpus.wiki")

PAGE_TYPE_TO_BUCKET = {
    "entity": "entities",
    "concept": "concepts",
    "playbook": "playbooks",
}


def _default_tenant() -> str:
    return os.getenv("SOLOMON_TENANT_ID", "default")


# ---------------------------------------------------------------------------
# Page-path helpers
# ---------------------------------------------------------------------------


def page_path(page_type: str, slug: str) -> Path:
    """Resolve <corpus_root>/wiki/<entities|concepts|playbooks>/<slug>.md."""
    if page_type not in PAGE_TYPE_TO_BUCKET:
        raise ValueError(f"unknown page_type: {page_type!r}")
    return corpus_root() / "wiki" / PAGE_TYPE_TO_BUCKET[page_type] / f"{slug}.md"


def read_page(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_page(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Section parsing + hashing
# ---------------------------------------------------------------------------


_HEADING_SPLIT_RE = re.compile(r"(?m)^## ")


def split_sections(content: str) -> List[Tuple[str, str]]:
    """Return [(header_line, section_body)].

    Front-matter / intro before the first ``## `` becomes one synthetic
    section with header ``__preface__``. Each subsequent ``## ...``
    starts a new section.
    """
    if not content.strip():
        return []
    sections: List[Tuple[str, str]] = []
    parts = _HEADING_SPLIT_RE.split(content)
    if parts and parts[0].strip():
        sections.append(("__preface__", parts[0]))
    for part in parts[1:]:
        lines = part.split("\n", 1)
        header = "## " + lines[0]
        body = lines[1] if len(lines) > 1 else ""
        sections.append((header, body))
    return sections


def section_hash(header: str, body: str) -> str:
    return hashlib.sha256(f"{header}\n{body}".encode("utf-8")).hexdigest()


def hashes_for_page(content: str) -> List[Tuple[str, str, str, str]]:
    """Return [(section_hash, header, body, full_section_text)]."""
    out = []
    for header, body in split_sections(content):
        h = section_hash(header, body)
        full = body if header == "__preface__" else f"{header}\n{body}"
        out.append((h, header, body, full))
    return out


# ---------------------------------------------------------------------------
# wiki_vectors persistence
# ---------------------------------------------------------------------------


def previous_hashes(page_path_str: str, *, tenant_id: Optional[str] = None) -> List[str]:
    tid = tenant_id or _default_tenant()
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT section_hashes FROM wiki_vectors "
                "WHERE page_path = ? AND tenant_id = ?",
                (page_path_str, tid),
            )
            row = cur.fetchone()
    if not row:
        return []
    parsed = parse_json(row[0])
    return parsed if isinstance(parsed, list) else []


def upsert_hashes(page_path_str: str, hashes: List[str], *, tenant_id: Optional[str] = None) -> None:
    tid = tenant_id or _default_tenant()
    now = datetime.utcnow().isoformat() + "Z"
    with get_conn() as conn:
        with cursor(conn) as cur:
            # delete-then-insert is portable across SQLite + Postgres
            execute(
                cur,
                "DELETE FROM wiki_vectors WHERE page_path = ? AND tenant_id = ?",
                (page_path_str, tid),
            )
            execute(
                cur,
                "INSERT INTO wiki_vectors (page_path, tenant_id, section_hashes, last_updated) "
                "VALUES (?, ?, ?, ?)",
                (page_path_str, tid, jsonify(hashes), now),
            )
        conn.commit()


def delete_page_hashes(page_path_str: str, *, tenant_id: Optional[str] = None) -> None:
    tid = tenant_id or _default_tenant()
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "DELETE FROM wiki_vectors WHERE page_path = ? AND tenant_id = ?",
                (page_path_str, tid),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Embed-and-upsert: the section-hash diff core
# ---------------------------------------------------------------------------


def embed_and_upsert_page(
    *,
    page_type: str,
    slug: str,
    page_path_str: str,
    new_content: str,
    tenant_id: Optional[str] = None,
) -> int:
    """Section-hash the new content; diff against the previous hashes;
    delete embeddings rows for gone sections; embed + insert new sections.

    Returns the number of NEW section embeddings written (i.e. sections
    that didn't exist before).
    """
    tid = tenant_id or _default_tenant()
    new_sections = hashes_for_page(new_content)
    new_hashes = [h for (h, _hdr, _body, _full) in new_sections]
    prev = set(previous_hashes(page_path_str, tenant_id=tid))
    current = set(new_hashes)

    # Orphan cleanup: hashes that were live but aren't any more.
    gone = sorted(prev - current)
    if gone:
        ids_to_delete = [f"wiki:{slug}:{h}" for h in gone]
        try:
            corpus_embed.delete_by_source_ids(
                corpus_embed.SOURCE_TABLE_CORPUS_WIKI,
                ids_to_delete,
                tenant_id=tid,
            )
        except Exception:  # noqa: BLE001
            logger.exception("orphan-vector delete failed for %s", page_path_str)

    # Embed only sections that are genuinely new (or whose body changed).
    new_only = [
        (h, full, header)
        for (h, header, _body, full) in new_sections
        if h not in prev and full.strip()
    ]
    written = 0
    for h, full, header in new_only:
        try:
            eid = corpus_embed.store_section_embedding(
                source_id=f"wiki:{slug}:{h}",
                text=full,
                source_table=corpus_embed.SOURCE_TABLE_CORPUS_WIKI,
                metadata={
                    "page_type": page_type,
                    "slug": slug,
                    "wiki_path": page_path_str,
                    "section_hash": h,
                    "section_header": header,
                    "ingested_at": datetime.utcnow().isoformat() + "Z",
                },
                tenant_id=tid,
            )
            if eid is not None:
                written += 1
        except Exception:  # noqa: BLE001
            logger.exception("wiki embed/upsert failed for %s section %s",
                             page_path_str, h)

    upsert_hashes(page_path_str, new_hashes, tenant_id=tid)
    return written


# ---------------------------------------------------------------------------
# Cascade helper used by forget.py
# ---------------------------------------------------------------------------


def remove_page(
    *,
    page_type: str,
    slug: str,
    page_path_str: str,
    tenant_id: Optional[str] = None,
) -> int:
    """Delete the wiki page file, all embeddings rows, and the wiki_vectors
    entry. Returns the number of embedding rows removed.
    """
    tid = tenant_id or _default_tenant()
    prev = previous_hashes(page_path_str, tenant_id=tid)
    removed = 0
    if prev:
        ids = [f"wiki:{slug}:{h}" for h in prev]
        try:
            removed = corpus_embed.delete_by_source_ids(
                corpus_embed.SOURCE_TABLE_CORPUS_WIKI, ids, tenant_id=tid
            )
        except Exception:  # noqa: BLE001
            logger.exception("delete_by_source_ids failed for %s", page_path_str)
    delete_page_hashes(page_path_str, tenant_id=tid)
    # Delete the file on disk if it still exists.
    try:
        path = page_path(page_type, slug)
        if path.exists():
            path.unlink()
    except Exception:  # noqa: BLE001
        logger.exception("could not unlink wiki page %s/%s", page_type, slug)
    return removed

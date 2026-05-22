"""Corpus ingestion orchestrator.

Walks one file (or a directory) through:

  1. Size check vs schema_config.file_limits().
  2. SHA256 → manifest dedup.  If already 'success', no-op.
  3. Route to a category (route.route).
  4. Extract text (extract.extract).
  5. Insert pending manifest row, mark in_progress.
  6. Redact (sensitivity_filter.scrub).
  7. Write the redacted text to corpus/raw/<category>/<slug>.
  8. Chunk → embed → store corpus_raw embeddings.
  9. Pass 1 (extract envelope) + Pass 2 (per-wiki-page merge + embed +
     orphan-vector cleanup) via llm_passes.
 10. Write proposed_rules + mentoring_queue rows via rules.
 11. Mark the manifest row 'success' (or 'partial' / 'failed').
 12. Log to ingestion_jobs / ingestion_documents (existing tables).

Failure handling per REPORT-CORPUS.md §4.7: any non-fatal exception in
the LLM passes leaves the raw vectors in place and the manifest row in
'partial'. Crash-mid-run is recoverable because re-ingesting a SHA that
already has a 'partial' row picks up where it left off (in_progress is
ignored — we treat partial like pending for retry purposes).

Public surface:

  - ``ingest_file(path) -> IngestResult``       — process one file.
  - ``ingest_directory(path) -> List[IngestResult]`` — recursive batch.

Both honour ``SOLOMON_CORPUS_ROOT`` and ``SOLOMON_TENANT_ID``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import embed as corpus_embed
from . import extract as corpus_extract
from . import llm_passes
from . import manifest as cm
from . import rules as cr
from .chunk import chunk as do_chunk
from .route import route
from .schema_config import corpus_root, file_limits
from ..ingestion.sensitivity_filter import scrub
from ..storage.pool import cursor, execute, get_conn

logger = logging.getLogger("solomon.corpus.ingest")


@dataclass
class IngestResult:
    status: str               # 'success' | 'partial' | 'failed' | 'skipped' | 'parked'
    reason: Optional[str] = None
    sha256: Optional[str] = None
    raw_path: Optional[str] = None
    category: Optional[str] = None
    file_id: Optional[str] = None
    vector_count: int = 0
    wiki_pages: List[Dict[str, Any]] = field(default_factory=list)
    rules_written: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SLUGIFY_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _default_tenant() -> str:
    return os.getenv("SOLOMON_TENANT_ID", "default")


def _slugify(stem: str) -> str:
    s = _SLUGIFY_RE.sub("-", stem).strip("-").lower()
    return s or "file"


def _normalized_filename(path: Path, sha: str) -> str:
    return f"{_slugify(path.stem)}-{sha[:8]}{path.suffix.lower()}"


def _park(path: Path, target_subdir: str, reason: str) -> Path:
    inbox = corpus_root() / "inbox"
    target_dir = inbox / target_subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / path.name
    if dest.exists():
        dest = target_dir / f"{path.stem}-{uuid.uuid4().hex[:6]}{path.suffix}"
    try:
        shutil.move(str(path), str(dest))
    except Exception:  # noqa: BLE001
        logger.exception("failed to park %s -> %s (reason=%s)", path, dest, reason)
    return dest


def _ensure_default_tenant() -> None:
    """Ensure the default tenant row exists. Idempotent."""
    tid = _default_tenant()
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO tenants (tenant_id, business_name) "
                "VALUES (?, ?) ON CONFLICT (tenant_id) DO NOTHING",
                (tid, "Solomon"),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Main entry: ingest_file
# ---------------------------------------------------------------------------


def ingest_file(path: Path | str, *, tenant_id: Optional[str] = None) -> IngestResult:
    """Process one file. Idempotent on SHA — re-ingesting a 'success' file
    is a no-op.

    On any unexpected exception we return ``status='failed'`` rather than
    propagating so a batch run can continue across the rest of the files.
    """
    src = Path(path)
    tid = tenant_id or _default_tenant()
    _ensure_default_tenant()

    if not src.exists() or not src.is_file():
        return IngestResult(status="failed", reason=f"not a file: {src}")

    # 1. Size check.
    try:
        size = src.stat().st_size
    except OSError as e:
        return IngestResult(status="failed", reason=f"stat: {e}")
    max_bytes = int(file_limits().get("max_size_bytes", 100 * 1024 * 1024))
    if size > max_bytes:
        _park(src, "_oversized", f"{size}b > {max_bytes}b")
        return IngestResult(status="parked", reason="oversized")

    # 2. SHA + dedup.
    sha = cm.file_sha256(src)
    if cm.is_already_ingested(sha, tenant_id=tid):
        return IngestResult(status="skipped", reason="already_ingested", sha256=sha)

    # 3. Route.
    category = route(src)
    if not category:
        _park(src, "_unsupported", f"no route for {src.suffix}")
        return IngestResult(status="parked", reason="unrouted")

    # 4. Extract.
    try:
        doc = corpus_extract.extract(src)
        text = doc.text
    except corpus_extract.UnsupportedFileType as e:
        _park(src, "_unsupported", str(e))
        return IngestResult(status="parked", reason="unsupported", sha256=sha)
    except Exception as e:  # noqa: BLE001
        logger.exception("extract failed for %s", src)
        return IngestResult(status="failed", reason=f"extract: {e}", sha256=sha)

    # 5. Manifest pending row.
    file_id = cm.insert_pending(
        sha=sha, inbox_path=str(src), size_bytes=size, category=category, tenant_id=tid
    )
    cm.mark_in_progress(file_id)

    # 6. Redact.
    try:
        scrub_result = scrub(text)
        text = getattr(scrub_result, "redacted_text", None) or text
    except Exception as e:  # noqa: BLE001
        logger.exception("redact failed for %s", src)
        cm.mark_failed(file_id, f"redact: {e}")
        return IngestResult(status="failed", reason="redact", sha256=sha, file_id=file_id)

    # 7. Write raw bytes to corpus/raw/<category>/<slug>.
    raw_dir = corpus_root() / "raw" / category
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_filename = _normalized_filename(src, sha)
    raw_path_abs = raw_dir / raw_filename
    raw_path_abs.write_text(text, encoding="utf-8")
    try:
        rel_raw = str(raw_path_abs.relative_to(corpus_root().parent))
    except ValueError:
        rel_raw = str(raw_path_abs)

    # Best-effort: remove the inbox copy.
    if src != raw_path_abs:
        try:
            src.unlink()
        except OSError:
            pass

    # 8. Chunk + embed.
    document_type = _document_type_for(category, src.suffix.lower())
    chunks = do_chunk(text, document_type)
    vector_count = 0
    if chunks:
        try:
            ids = corpus_embed.store_chunk_embeddings(
                source_id_prefix=f"raw:{sha[:12]}",
                chunks=chunks,
                source_table=corpus_embed.SOURCE_TABLE_CORPUS_RAW,
                extra_metadata={
                    "category": category,
                    "raw_path": rel_raw,
                    "sha256": sha,
                    "document_type": document_type,
                },
                tenant_id=tid,
            )
            vector_count = len(ids)
        except Exception as e:  # noqa: BLE001
            logger.exception("embed/store failed for %s", src)
            cm.mark_partial(file_id, rel_raw, 0, f"embed: {e}")
            return IngestResult(
                status="partial",
                reason="embed",
                sha256=sha,
                raw_path=rel_raw,
                category=category,
                file_id=file_id,
            )

    # 9-10. LLM passes — best-effort.
    touched_pages: List[Dict[str, Any]] = []
    rules_written = 0
    try:
        envelope = llm_passes.extract(text, category=category, raw_path=rel_raw)
        if envelope:
            touched_pages = llm_passes.merge_pages(
                envelope=envelope, raw_path_rel=rel_raw
            )
            llm_passes.append_index(touched_pages)
            rules_written = cr.write_proposed_rules(
                proposals=envelope.get("proposed_rules", []) or [],
                source_path=rel_raw,
                tenant_id=tid,
            )
    except Exception as e:  # noqa: BLE001
        logger.exception("LLM passes failed for %s", src)
        cm.mark_partial(file_id, rel_raw, vector_count, f"llm_passes: {e}")
        return IngestResult(
            status="partial",
            reason="llm_passes",
            sha256=sha,
            raw_path=rel_raw,
            category=category,
            file_id=file_id,
            vector_count=vector_count,
        )

    # 11. Mark success.
    cm.mark_success(
        file_id,
        raw_path=rel_raw,
        vector_count=vector_count,
        wiki_pages_touched=[str(p.get("page_path")) for p in touched_pages if p.get("page_path")],
    )

    logger.info(
        "ingested %s -> %s (%d vectors, %d wiki pages, %d rules)",
        src.name, rel_raw, vector_count, len(touched_pages), rules_written,
    )

    return IngestResult(
        status="success",
        sha256=sha,
        raw_path=rel_raw,
        category=category,
        file_id=file_id,
        vector_count=vector_count,
        wiki_pages=touched_pages,
        rules_written=rules_written,
    )


# ---------------------------------------------------------------------------
# Batch wrapper
# ---------------------------------------------------------------------------


def ingest_directory(
    path: Path | str,
    *,
    recursive: bool = True,
    tenant_id: Optional[str] = None,
) -> List[IngestResult]:
    """Walk ``path`` and ingest every file we find. Skips hidden and
    parking subdirectories (``_oversized`` / ``_unsupported`` /
    ``_pre-redaction``).
    """
    root = Path(path)
    if not root.exists():
        return []
    out: List[IngestResult] = []
    skip_dirs = {"_oversized", "_unsupported", "_pre-redaction", "_forgotten"}
    iterator = root.rglob("*") if recursive else root.iterdir()
    for p in iterator:
        if not p.is_file():
            continue
        if p.name.startswith("."):
            continue
        if any(part in skip_dirs for part in p.parts):
            continue
        out.append(ingest_file(p, tenant_id=tenant_id))
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_CATEGORY_TO_DOC_TYPE = {
    "sops": "sop",
    "emails": "email_thread",
    "messages": "transcript",
    "docs": "internal_doc",
    "data": "other",
}


def _document_type_for(category: str, suffix: str) -> str:
    """Map a corpus category + file suffix to the ingestion chunker's
    document_type vocabulary. The chunker falls back to sliding window
    for anything not in TYPE_AWARE_KINDS, so an imperfect match here is
    fine — but pickng a good match unlocks the type-aware chunker.
    """
    base = _CATEGORY_TO_DOC_TYPE.get(category, "other")
    # Email file → email_thread regardless of category.
    if suffix in {".eml", ".mbox"}:
        return "email_thread"
    return base

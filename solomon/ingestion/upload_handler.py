"""Upload handler / orchestrator. Runs the full ingestion pipeline for a
batch of documents.

Stages (per document):
  1. Load text from disk
  2. Sensitivity filter (PII redaction + skip-if-flagged)
  3. Classify (type, period, participants, domain, salience estimate)
  4. Chunk by type
  5. Embed each chunk (local sentence-transformers by default)
  6. Extract decisions from each chunk (deep LLM, gated by salience)
  7. Store decisions with historical=true

After all documents in a batch are processed:
  8. Mine heuristics across the batch (one cross-document pass)
  9. Cross-reference documents
  10. Owner reviews via `solomon ingestion review`

Budget guarded throughout: if the per-tenant monthly cap is hit, stages
4-9 pause and the owner is notified.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import queue_documents  # the simple queueing helper already in __init__.py
from .budget_tracker import can_spend, monthly_cap_tokens, record_spend
from .chunker import chunk_document
from .classifier import classify_document
from .cross_referencer import find_references, store_references
from .embedder import embed_batch, store_embedding
from .extractor import extract_from_chunk, store_extracted_decision
from .heuristic_miner import mine_batch
from .sensitivity_filter import scrub

from ..storage.pool import get_pool
from ..storage.decisions import get_or_create_tenant_id

logger = logging.getLogger("solomon.ingestion.upload_handler")


def ingest_paths(
    paths: List[str],
    tenant_id: Optional[str] = None,
    flagged_sensitive_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run the full pipeline on a list of file paths.

    Returns a summary dict with counts of documents processed, decisions
    extracted, embeddings stored, heuristics proposed, and any errors.
    """
    if tenant_id is None:
        tenant_id = get_or_create_tenant_id()
    flagged_set = set(flagged_sensitive_paths or [])

    # Queue the documents (creates the ingestion_jobs + ingestion_documents rows).
    job_id = queue_documents(paths, tenant_id=tenant_id)

    summary: Dict[str, Any] = {
        "job_id": job_id,
        "tenant_id": tenant_id,
        "documents_processed": 0,
        "documents_skipped": 0,
        "decisions_extracted": 0,
        "embeddings_stored": 0,
        "heuristics_proposed": 0,
        "errors": [],
    }

    document_metadatas: List[Dict[str, Any]] = []

    for path in paths:
        try:
            res = _process_one(path, tenant_id, job_id, flagged=path in flagged_set)
            summary["documents_processed"] += 1
            summary["decisions_extracted"] += res["decisions_extracted"]
            summary["embeddings_stored"] += res["embeddings_stored"]
            document_metadatas.append(res["document_metadata"])
        except _SkipDocument as e:
            summary["documents_skipped"] += 1
            logger.info("Skipped %s: %s", path, e)
        except Exception as e:  # noqa: BLE001
            logger.exception("Document failed: %s", path)
            summary["errors"].append({"path": path, "reason": str(e)})

    # Cross-document passes.
    if summary["documents_processed"] > 0:
        try:
            mined = mine_batch(tenant_id, job_id)
            summary["heuristics_proposed"] = len(mined)
        except Exception as e:  # noqa: BLE001
            logger.warning("Heuristic mining failed: %s", e)
            summary["errors"].append({"stage": "mine_batch", "reason": str(e)})

        try:
            refs = find_references(document_metadatas)
            store_references(tenant_id, refs)
        except Exception as e:  # noqa: BLE001
            logger.warning("Cross-referencer failed: %s", e)

    # Mark the job done.
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE ingestion_jobs SET status=%s, finished_at=%s WHERE job_id=%s;",
                    ("done", datetime.now(timezone.utc), job_id),
                )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not mark job %d as done: %s", job_id, e)

    return summary


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

class _SkipDocument(Exception):
    pass


def _process_one(path: str, tenant_id: str, job_id: int, flagged: bool = False) -> Dict[str, Any]:
    """Run stages 1-7 on a single document."""
    p = Path(path)
    if not p.exists():
        raise _SkipDocument("file not found")
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:  # noqa: BLE001
        raise _SkipDocument(f"read failed: {e}") from e

    # Stage 2: sensitivity filter.
    sens = scrub(text, document_flagged_sensitive=flagged)
    if sens.skip_document:
        raise _SkipDocument("owner-flagged sensitive")
    safe_text = sens.redacted_text

    # Stage 3: classify.
    cls = classify_document(safe_text, filename=p.name)

    # Persist the ingestion_documents row metadata.
    document_id = _upsert_document(tenant_id, job_id, str(p), cls)

    # Stage 4: chunk.
    chunks = chunk_document(safe_text, cls.document_type)
    if not chunks:
        raise _SkipDocument("no chunks produced")

    # Budget guard for embed + extract.
    # Rough estimate: embed = ~1 token per 4 chars, extract uses ~1500 tokens per chunk.
    est_chars = sum(len(c.text) for c in chunks)
    est_tokens = int(est_chars / 4) + (1500 * len(chunks))
    if not can_spend(tenant_id, est_tokens):
        logger.warning("Skipping deep stages on %s: budget cap reached.", path)
        raise _SkipDocument("budget cap reached")

    # Stage 5: embed.
    embeddings_stored = 0
    chunk_texts = [c.text for c in chunks]
    vectors = embed_batch(chunk_texts)
    for chunk, vec in zip(chunks, vectors):
        if vec is None:
            continue
        # Store with source_table='ingestion_chunk' and source_id=document_id for now.
        # Later: introduce a chunks table with its own ids if we need chunk-level
        # provenance separate from documents.
        emb_id = store_embedding(tenant_id, "ingestion_chunk", document_id * 10000 + chunk.seq, vec)
        if emb_id:
            embeddings_stored += 1

    # Stage 6: decision extraction (only on chunks the salience estimate flags
    # as worth deep work).
    decisions_extracted = 0
    if cls.salience_estimate >= 0.3:
        doc_meta = {
            "document_id": document_id,
            "document_type": cls.document_type,
            "domain": cls.domain,
            "period_start": cls.period_start.isoformat() if cls.period_start else None,
            "participants": cls.participants,
        }
        for chunk in chunks:
            extracted = extract_from_chunk(chunk.text, doc_meta)
            if extracted is None:
                continue
            decision_id = store_extracted_decision(
                tenant_id=tenant_id,
                document_id=document_id,
                extracted=extracted,
                scope=cls.domain,
                domain=cls.domain,
            )
            if decision_id:
                decisions_extracted += 1

    record_spend(tenant_id, est_tokens)

    return {
        "document_id": document_id,
        "decisions_extracted": decisions_extracted,
        "embeddings_stored": embeddings_stored,
        "document_metadata": {
            "document_id": document_id,
            "filename": p.name,
            "document_type": cls.document_type,
            "subject": _extract_subject(safe_text),
            "channel_metadata": {},  # populated later when we read true email headers
            "participants": cls.participants,
        },
    }


def _upsert_document(tenant_id: str, job_id: int, path: str, cls) -> int:  # noqa: ANN001
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingestion_documents (
                    job_id, tenant_id, storage_path, document_type,
                    period_start, period_end, participants, domain, salience_estimate, status
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s
                ) RETURNING document_id;
                """,
                (
                    job_id, tenant_id, path, cls.document_type,
                    cls.period_start, cls.period_end,
                    _to_json(cls.participants),
                    cls.domain, cls.salience_estimate, "processing",
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return int(row[0]) if row else 0


def _to_json(value) -> str:  # noqa: ANN001
    import json
    return json.dumps(value)


def _extract_subject(text: str) -> str:
    """Pull the first 'Subject:' line we find, used for cross-referencing."""
    for line in text.splitlines()[:20]:
        if line.lower().startswith("subject:"):
            return line.split(":", 1)[1].strip()
    return ""

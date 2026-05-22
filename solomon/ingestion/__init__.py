"""Ingestion pipeline (Part 26 of the design doc).

Bulk and ad-hoc absorption of historical documents — old email threads,
proposals, meeting transcripts, contracts, SOPs, customer feedback,
internal docs, text exchanges, notebooks, call recordings.

Pipeline stages (per document):
  1. Upload and queue
  2. Type classification
  3. Chunking and embedding
  4. Decision extraction
  5. Heuristic mining
  6. Cross-referencing
  7. Owner review

For Phase 1 we expose the queue + classifier + chunker scaffolding. The
real LLM-driven extraction and mining are TODOs that the upcoming Phase 2
work will fill in. The DB tables (ingestion_jobs, ingestion_documents)
are already in schema.sql.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("solomon.ingestion")


def queue_documents(paths: List[str], tenant_id: Optional[str] = None) -> int:
    """Queue one or more documents for ingestion. Returns job_id."""
    from .storage.pool import get_pool
    from .storage.decisions import get_or_create_tenant_id

    if tenant_id is None:
        tenant_id = get_or_create_tenant_id()

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ingestion_jobs (tenant_id, status, document_count) "
                "VALUES (%s, 'queued', %s) RETURNING job_id;",
                (tenant_id, len(paths)),
            )
            row = cur.fetchone()
            job_id = int(row[0]) if row else 0
            for p in paths:
                cur.execute(
                    "INSERT INTO ingestion_documents (job_id, tenant_id, storage_path) "
                    "VALUES (%s, %s, %s);",
                    (job_id, tenant_id, p),
                )
        conn.commit()
    logger.info("Queued ingestion job %d with %d documents.", job_id, len(paths))
    return job_id


def list_pending_jobs(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    from .storage.pool import get_pool
    from .storage.decisions import get_or_create_tenant_id
    if tenant_id is None:
        tenant_id = get_or_create_tenant_id()
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_id, status, document_count, created_at "
                "FROM ingestion_jobs WHERE tenant_id=%s AND status IN ('queued','running') "
                "ORDER BY created_at DESC;",
                (tenant_id,),
            )
            rows = cur.fetchall()
    return [
        {"job_id": r[0], "status": r[1], "document_count": r[2], "created_at": r[3].isoformat()}
        for r in rows
    ]


# TODO Phase 2 implementations:
#  - classify_document: detect document type, period, participants, domain
#  - chunk_document: type-specific chunking
#  - embed_chunks: pgvector embeddings
#  - extract_decisions: pull retrospective decisions from chunks
#  - mine_heuristics: cross-document pattern detection
#  - cross_reference: link related documents
#  - sensitivity_filter: PII redaction before embedding
#  - review_queue: owner UI integration
#  - budget_tracker: token cost cap

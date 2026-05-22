"""Corpus health checks — orphan embeddings, broken file refs, stale wiki.

REPORT-CORPUS.md §1.12 + §4.5 Phase C. Sleep-cycle job 9 will call this;
the CLI ``solomon corpus stats`` shows a summary count.

What we check (deterministic, no LLM):

  1. **Orphan embeddings** — corpus_raw rows whose ``raw_path`` no longer
     exists on disk, OR whose source_id doesn't trace back to an
     ingested_files row marked ``success``.
  2. **Broken wiki refs** — wiki_vectors rows whose page file is missing
     from corpus/wiki/.
  3. **Orphan wiki vectors** — corpus_wiki embeddings rows whose slug
     doesn't have a matching file on disk.
  4. **Forgotten files with lingering rows** — ingested_files in
     ``forgotten`` state that still have embeddings.
  5. **Queued proposed rules pointing at missing files** — proposed_rules
     whose source_path file is gone.

Each check returns a list of ``LintFinding`` dataclasses with a
``severity`` ('warn' / 'error'), a ``code``, and free-form ``detail``.
``run_lint()`` aggregates everything and returns the list. Caller
decides whether to print, surface to mentoring_queue, etc.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import embed as corpus_embed
from .schema_config import corpus_root
from ..storage.pool import cursor, execute, get_conn, parse_json

logger = logging.getLogger("solomon.corpus.lint")


@dataclass
class LintFinding:
    code: str           # short identifier (e.g. "orphan_raw_embedding")
    severity: str       # "warn" | "error"
    detail: str         # human-readable explanation
    target: Optional[str] = None    # e.g. embedding source_id, path, rule id
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _default_tenant() -> str:
    return os.getenv("SOLOMON_TENANT_ID", "default")


def _root_for_relative(rel_path: str) -> Path:
    """Resolve a 'corpus/raw/...' relative path against the right root."""
    cr = corpus_root()
    # If the rel_path begins with 'corpus/', it's relative to the parent of
    # corpus_root(); otherwise it's relative to corpus_root() itself.
    if rel_path.startswith("corpus/"):
        return cr.parent / rel_path
    return cr / rel_path


def find_orphan_raw_embeddings(*, tenant_id: Optional[str] = None) -> List[LintFinding]:
    """corpus_raw rows whose raw_path metadata points at a missing file."""
    tid = tenant_id or _default_tenant()
    findings: List[LintFinding] = []
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT source_id, metadata FROM embeddings "
                "WHERE tenant_id = ? AND source_table = ?",
                (tid, corpus_embed.SOURCE_TABLE_CORPUS_RAW),
            )
            rows = cur.fetchall()
    for row in rows:
        meta = parse_json(row[1]) or {}
        raw_path = meta.get("raw_path")
        if not raw_path:
            findings.append(LintFinding(
                code="orphan_raw_embedding_no_path",
                severity="warn",
                detail=f"corpus_raw embedding {row[0]} has no raw_path in metadata",
                target=row[0],
            ))
            continue
        if not _root_for_relative(raw_path).exists():
            findings.append(LintFinding(
                code="orphan_raw_embedding",
                severity="warn",
                detail=f"corpus_raw embedding points at missing file {raw_path}",
                target=row[0],
                metadata={"raw_path": raw_path},
            ))
    return findings


def find_broken_wiki_pages(*, tenant_id: Optional[str] = None) -> List[LintFinding]:
    """wiki_vectors rows whose page_path file doesn't exist on disk."""
    tid = tenant_id or _default_tenant()
    findings: List[LintFinding] = []
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT page_path FROM wiki_vectors WHERE tenant_id = ?",
                (tid,),
            )
            rows = cur.fetchall()
    for row in rows:
        page_path = row[0]
        if not _root_for_relative(page_path).exists():
            findings.append(LintFinding(
                code="broken_wiki_page",
                severity="error",
                detail=f"wiki_vectors row references missing page {page_path}",
                target=page_path,
            ))
    return findings


def find_orphan_wiki_embeddings(*, tenant_id: Optional[str] = None) -> List[LintFinding]:
    """corpus_wiki embeddings rows whose page file is missing."""
    tid = tenant_id or _default_tenant()
    findings: List[LintFinding] = []
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT source_id, metadata FROM embeddings "
                "WHERE tenant_id = ? AND source_table = ?",
                (tid, corpus_embed.SOURCE_TABLE_CORPUS_WIKI),
            )
            rows = cur.fetchall()
    for row in rows:
        meta = parse_json(row[1]) or {}
        wiki_path = meta.get("wiki_path")
        if not wiki_path:
            continue  # bestowed empty: skip silently — bad metadata caught elsewhere
        if not _root_for_relative(wiki_path).exists():
            findings.append(LintFinding(
                code="orphan_wiki_embedding",
                severity="warn",
                detail=f"corpus_wiki embedding points at missing page {wiki_path}",
                target=row[0],
                metadata={"wiki_path": wiki_path},
            ))
    return findings


def find_forgotten_with_embeddings(*, tenant_id: Optional[str] = None) -> List[LintFinding]:
    """ingested_files marked forgotten but with embeddings rows still pointing
    at their raw_path."""
    tid = tenant_id or _default_tenant()
    findings: List[LintFinding] = []
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT id, raw_path FROM ingested_files "
                "WHERE tenant_id = ? AND status = 'forgotten'",
                (tid,),
            )
            rows = cur.fetchall()
    for row in rows:
        file_id = row[0]
        raw_path = row[1]
        if not raw_path:
            continue
        with get_conn() as conn2:
            with cursor(conn2) as cur2:
                execute(
                    cur2,
                    "SELECT COUNT(*) FROM embeddings "
                    "WHERE tenant_id = ? AND source_table = ? "
                    "AND metadata LIKE ?",
                    (tid, corpus_embed.SOURCE_TABLE_CORPUS_RAW, f"%{raw_path}%"),
                )
                cnt = int(cur2.fetchone()[0])
        if cnt:
            findings.append(LintFinding(
                code="forgotten_with_embeddings",
                severity="error",
                detail=(
                    f"ingested_files row {file_id} marked forgotten but "
                    f"{cnt} embeddings rows still reference {raw_path}"
                ),
                target=file_id,
                metadata={"raw_path": raw_path, "count": cnt},
            ))
    return findings


def find_orphan_proposed_rules(*, tenant_id: Optional[str] = None) -> List[LintFinding]:
    """queued proposed_rules whose source_path file is gone."""
    tid = tenant_id or _default_tenant()
    findings: List[LintFinding] = []
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT id, source_path FROM proposed_rules "
                "WHERE tenant_id = ? AND status = 'queued'",
                (tid,),
            )
            rows = cur.fetchall()
    for row in rows:
        pr_id, src = row[0], row[1]
        if not src:
            continue
        if not _root_for_relative(src).exists():
            findings.append(LintFinding(
                code="orphan_proposed_rule",
                severity="warn",
                detail=f"queued proposed_rule {pr_id} points at missing source {src}",
                target=pr_id,
                metadata={"source_path": src},
            ))
    return findings


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def run_lint(*, tenant_id: Optional[str] = None) -> List[LintFinding]:
    """Run every check and return a single flat list of findings."""
    tid = tenant_id or _default_tenant()
    out: List[LintFinding] = []
    for check in (
        find_orphan_raw_embeddings,
        find_broken_wiki_pages,
        find_orphan_wiki_embeddings,
        find_forgotten_with_embeddings,
        find_orphan_proposed_rules,
    ):
        try:
            out.extend(check(tenant_id=tid))
        except Exception:  # noqa: BLE001
            logger.exception("lint check %s failed", check.__name__)
    return out


def summary(findings: List[LintFinding]) -> Dict[str, int]:
    """Roll findings up by code for terse CLI output."""
    counts: Dict[str, int] = {}
    for f in findings:
        counts[f.code] = counts.get(f.code, 0) + 1
    counts["total"] = sum(counts.values())
    counts["errors"] = sum(1 for f in findings if f.severity == "error")
    counts["warnings"] = sum(1 for f in findings if f.severity == "warn")
    return counts

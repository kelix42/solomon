"""Surface owner rules buried in corpus material.

THE CRITICAL FILE. This is what closes the onboarding loop:
bulk historical material (SOPs, emails, transcripts) can teach Solomon
the same rules the interview engine extracts, but ONLY after the owner
reviews them. Pass 1 detects first-person rules; this module writes
them as ``proposed_rules`` rows AND a paired ``mentoring_queue`` row
(``source='corpus_rule_proposal'``, priority 4) so the next mentoring
session walks the owner through them.

Per REPORT-CORPUS.md §1.5:

  Dedup key = sha256(verbatim_excerpt) + source_path.
  If a (source_path, verbatim_excerpt) pair already exists with any
  status (queued / approved / rejected / dismissed), we skip — the
  owner has either acted on it or it's already waiting for them.

Idempotency matters: re-ingesting the same file (via SHA dedup miss
plus a status='partial' retry) must NOT enqueue the same rule twice.

Schema reminder — the columns in ``proposed_rules``:
  id, tenant_id, domain, proposed_statement, verbatim_excerpt,
  source_path, keywords, confidence_hint, status, created_at
plus UNIQUE (source_path, verbatim_excerpt).

And ``mentoring_queue``:
  id, tenant_id, source, surfaced_at, status, priority, payload
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..storage.pool import cursor, execute, get_conn, jsonify

logger = logging.getLogger("solomon.corpus.rules")

VALID_DOMAINS = {"pricing", "hiring", "ops", "customer", "vendor", "finance"}
VALID_CONFIDENCE = {"stated", "repeated", "exemplified"}

MENTORING_QUEUE_SOURCE = "corpus_rule_proposal"
MENTORING_QUEUE_PRIORITY = 4


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _new_id() -> str:
    return uuid.uuid4().hex


def _default_tenant() -> str:
    return os.getenv("SOLOMON_TENANT_ID", "default")


def dedup_key(*, source_path: str, verbatim_excerpt: str) -> str:
    """The reference dedup key (sha256 of excerpt + source_path).

    The DB also enforces UNIQUE (source_path, verbatim_excerpt) directly,
    but exposing the hash makes test assertions and lint reporting easier.
    """
    payload = f"{source_path}\n{verbatim_excerpt}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _excerpt_seen(conn, *, tenant_id: str, source_path: str, verbatim: str) -> bool:
    with cursor(conn) as cur:
        execute(
            cur,
            "SELECT 1 FROM proposed_rules "
            "WHERE tenant_id = ? AND source_path = ? AND verbatim_excerpt = ? "
            "LIMIT 1",
            (tenant_id, source_path, verbatim),
        )
        return cur.fetchone() is not None


def _normalise(proposal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Clamp the raw LLM proposal to a valid row. Returns None to skip."""
    domain = (proposal.get("domain") or "").strip().lower()
    verbatim = (proposal.get("verbatim_excerpt") or "").strip()
    statement = (proposal.get("proposed_statement") or "").strip()
    confidence = (proposal.get("confidence_hint") or "stated").strip().lower()
    keywords = proposal.get("keywords") or []

    if not verbatim or not statement:
        return None
    if domain not in VALID_DOMAINS:
        return None
    if confidence not in VALID_CONFIDENCE:
        confidence = "stated"
    if not isinstance(keywords, list):
        keywords = []
    cleaned_keywords = [k.strip().lower() for k in keywords if isinstance(k, str) and k.strip()]

    return {
        "domain": domain,
        "proposed_statement": statement,
        "verbatim_excerpt": verbatim,
        "keywords": cleaned_keywords,
        "confidence_hint": confidence,
    }


def write_proposed_rules(
    *,
    proposals: List[Dict[str, Any]],
    source_path: str,
    tenant_id: Optional[str] = None,
) -> int:
    """Insert one proposed_rules + matching mentoring_queue row per valid
    new proposal. Returns the count actually written.

    Skipped:
      - malformed proposals (missing verbatim/statement, bad domain)
      - duplicates by (tenant_id, source_path, verbatim_excerpt)

    Writes are wrapped in a single transaction per proposal so a crash
    mid-batch leaves the DB consistent.
    """
    tid = tenant_id or _default_tenant()
    if not proposals:
        return 0
    written = 0
    now = _now()
    for raw in proposals:
        norm = _normalise(raw)
        if norm is None:
            continue
        with get_conn() as conn:
            if _excerpt_seen(
                conn,
                tenant_id=tid,
                source_path=source_path,
                verbatim=norm["verbatim_excerpt"],
            ):
                continue
            pr_id = _new_id()
            try:
                with cursor(conn) as cur:
                    execute(
                        cur,
                        "INSERT INTO proposed_rules "
                        "(id, tenant_id, domain, proposed_statement, "
                        " verbatim_excerpt, source_path, keywords, "
                        " confidence_hint, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                        (
                            pr_id,
                            tid,
                            norm["domain"],
                            norm["proposed_statement"],
                            norm["verbatim_excerpt"],
                            source_path,
                            jsonify(norm["keywords"]),
                            norm["confidence_hint"],
                            now,
                        ),
                    )
                    payload = {
                        "proposed_rule_id": pr_id,
                        "domain": norm["domain"],
                        "source_path": source_path,
                        "proposed_statement": norm["proposed_statement"],
                        "verbatim_excerpt": norm["verbatim_excerpt"],
                        "confidence_hint": norm["confidence_hint"],
                    }
                    execute(
                        cur,
                        "INSERT INTO mentoring_queue "
                        "(tenant_id, source, surfaced_at, status, priority, payload) "
                        "VALUES (?, ?, ?, 'queued', ?, ?)",
                        (
                            tid,
                            MENTORING_QUEUE_SOURCE,
                            now,
                            MENTORING_QUEUE_PRIORITY,
                            jsonify(payload),
                        ),
                    )
                conn.commit()
                written += 1
            except Exception as e:  # noqa: BLE001
                # UNIQUE-violation race or DB hiccup — log and keep going.
                logger.warning(
                    "write_proposed_rules: skipping proposal due to %s",
                    e.__class__.__name__,
                )
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
    return written


def list_queued(
    *, tenant_id: Optional[str] = None, limit: int = 100
) -> List[Dict[str, Any]]:
    """Return queued rule proposals newest first, for the review CLI / lint."""
    tid = tenant_id or _default_tenant()
    out: List[Dict[str, Any]] = []
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT id, domain, proposed_statement, verbatim_excerpt, "
                "       source_path, keywords, confidence_hint, status, created_at "
                "FROM proposed_rules "
                "WHERE tenant_id = ? AND status = 'queued' "
                "ORDER BY created_at DESC "
                "LIMIT ?",
                (tid, limit),
            )
            rows = cur.fetchall()
    for r in rows:
        keys = [
            "id", "domain", "proposed_statement", "verbatim_excerpt",
            "source_path", "keywords", "confidence_hint", "status", "created_at",
        ]
        if hasattr(r, "keys"):
            d = {k: r[k] for k in keys if k in r.keys()}
        else:
            d = dict(zip(keys, r))
        try:
            import json
            d["keywords"] = json.loads(d.get("keywords") or "[]")
        except Exception:  # noqa: BLE001
            d["keywords"] = []
        out.append(d)
    return out


def mark_dismissed(proposal_id: str, *, tenant_id: Optional[str] = None) -> None:
    """Set status='dismissed' (used by lint or forget cascades)."""
    tid = tenant_id or _default_tenant()
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "UPDATE proposed_rules SET status = 'dismissed' "
                "WHERE id = ? AND tenant_id = ?",
                (proposal_id, tid),
            )
        conn.commit()


def delete_for_source(source_path: str, *, tenant_id: Optional[str] = None) -> int:
    """Delete proposed_rules + matching mentoring_queue rows for a source.

    Used by forget.py: when the owner asks to delete a corpus file we
    cascade and remove any rule proposals derived from it.
    Returns count of proposed_rules rows deleted.
    """
    tid = tenant_id or _default_tenant()
    with get_conn() as conn:
        # Collect IDs first so we can target the matching mentoring_queue rows.
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT id FROM proposed_rules "
                "WHERE tenant_id = ? AND source_path = ?",
                (tid, source_path),
            )
            ids = [r[0] for r in cur.fetchall()]
        # Remove the proposed_rules rows.
        with cursor(conn) as cur:
            execute(
                cur,
                "DELETE FROM proposed_rules "
                "WHERE tenant_id = ? AND source_path = ?",
                (tid, source_path),
            )
        # Remove mentoring_queue rows that reference these IDs.
        if ids:
            with cursor(conn) as cur:
                for pr_id in ids:
                    execute(
                        cur,
                        "DELETE FROM mentoring_queue "
                        "WHERE tenant_id = ? AND source = ? "
                        "AND payload LIKE ?",
                        (tid, MENTORING_QUEUE_SOURCE, f"%{pr_id}%"),
                    )
        conn.commit()
    return len(ids)

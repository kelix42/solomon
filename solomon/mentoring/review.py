"""Mentoring review CLI — walk the owner through queued rule proposals.

This module closes the corpus → owner loop. The corpus pipeline mines
first-person rules out of historical material and parks them in two
tables:

  - ``proposed_rules`` — the rule + verbatim_excerpt + source_path
  - ``mentoring_queue`` — paired row with ``source='corpus_rule_proposal'``
    and a ``payload`` JSON blob carrying ``proposed_rule_id``

This module reads the queue in (priority ASC, surfaced_at ASC) order
and lets the owner approve / reject / edit / skip each ``corpus_rule_proposal``.
Other mentoring_queue sources (contradiction / drift / promotion_ready /
demotion_alert) are noted and silently skipped — they get their own
review flows later.

Approve = INSERT a row into the real ``heuristics`` table (mirroring the
shape used by ``solomon.ingestion.review_queue.approve_heuristic`` —
``source='corpus_review'``, ``confidence=0.5``, ``status='active'``,
provenance carries the proposed_rule_id and source_path). The
proposed_rules row flips to ``approved`` and the mentoring_queue row
flips to ``resolved``.

Reject = proposed_rules → ``rejected``, mentoring_queue → ``dismissed``.

Edit = prompt for a new ``proposed_statement`` (default = existing),
then go down the approve path with the edited text.

Skip = leave both rows queued, move on.

Quit = exit cleanly, return whatever summary we have so far.

Storage rules of the road (per the solomon-project skill):
  - Pool API only: ``get_conn`` / ``cursor`` / ``execute`` / ``parse_json``.
  - ``?`` placeholders — never ``%s``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from ..storage.pool import cursor, execute, get_conn, jsonify, parse_json

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

CORPUS_RULE_SOURCE = "corpus_rule_proposal"
HEURISTIC_SOURCE = "corpus_review"
HEURISTIC_INITIAL_CONFIDENCE = 0.5
# Scope the corpus-mined rules live under. The corpus miner doesn't carry
# a scope through; "business" is the universal default until per-scope
# corpus mining lands.
HEURISTIC_SCOPE_DEFAULT = "business"


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _default_tenant() -> str:
    return os.getenv("SOLOMON_TENANT_ID", "default")


# ---------------------------------------------------------------------------
# data shapes
# ---------------------------------------------------------------------------


@dataclass
class ReviewSummary:
    """Per-session counters returned from ``run_review``."""

    approved: int = 0
    rejected: int = 0
    edited: int = 0
    skipped: int = 0
    quit: bool = False
    other_sources_skipped: int = 0
    inbox_zero: bool = False
    handled_sources: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DB reads
# ---------------------------------------------------------------------------


def _list_queue(tenant_id: str) -> List[Dict[str, Any]]:
    """Return queued mentoring_queue rows in (priority, surfaced_at) order.

    Includes ALL sources — the loop filters non-corpus ones itself so it
    can print a one-line note per skipped source.
    """
    out: List[Dict[str, Any]] = []
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT id, tenant_id, source, surfaced_at, status, "
                "       priority, payload "
                "FROM mentoring_queue "
                "WHERE tenant_id = ? AND status = 'queued' "
                "ORDER BY priority ASC, surfaced_at ASC, id ASC",
                (tenant_id,),
            )
            rows = cur.fetchall()
    keys = ["id", "tenant_id", "source", "surfaced_at",
            "status", "priority", "payload"]
    for r in rows:
        if hasattr(r, "keys"):
            d = {k: r[k] for k in keys if k in r.keys()}
        else:
            d = dict(zip(keys, r))
        d["payload"] = parse_json(d.get("payload")) or {}
        out.append(d)
    return out


def _get_proposed_rule(tenant_id: str, rule_id: str) -> Optional[Dict[str, Any]]:
    keys = ["id", "tenant_id", "domain", "proposed_statement",
            "verbatim_excerpt", "source_path", "keywords",
            "confidence_hint", "status", "created_at"]
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT id, tenant_id, domain, proposed_statement, "
                "       verbatim_excerpt, source_path, keywords, "
                "       confidence_hint, status, created_at "
                "FROM proposed_rules "
                "WHERE tenant_id = ? AND id = ?",
                (tenant_id, rule_id),
            )
            row = cur.fetchone()
    if row is None:
        return None
    if hasattr(row, "keys"):
        d = {k: row[k] for k in keys if k in row.keys()}
    else:
        d = dict(zip(keys, row))
    d["keywords"] = parse_json(d.get("keywords")) or []
    return d


# ---------------------------------------------------------------------------
# DB writes (per-action)
# ---------------------------------------------------------------------------


def _approve(
    *,
    tenant_id: str,
    rule: Dict[str, Any],
    queue_id: int,
    statement_override: Optional[str] = None,
) -> int:
    """Insert into heuristics, flip proposed_rules + mentoring_queue.

    Returns the new heuristic_id. Wrapped in a single transaction so a
    crash midway leaves the queue row queued (idempotent retry).
    """
    statement = (statement_override or rule["proposed_statement"]).strip()
    provenance = {
        "proposed_rule_id": rule["id"],
        "source_path": rule.get("source_path"),
        "verbatim_excerpt": rule.get("verbatim_excerpt"),
        "confidence_hint": rule.get("confidence_hint"),
        "domain": rule.get("domain"),
        "approved_at": _now(),
    }
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO heuristics "
                "(tenant_id, scope, domain, condition, action, "
                " confidence, source, provenance, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')",
                (
                    tenant_id,
                    HEURISTIC_SCOPE_DEFAULT,
                    rule.get("domain"),
                    rule.get("verbatim_excerpt") or statement,
                    statement,
                    HEURISTIC_INITIAL_CONFIDENCE,
                    HEURISTIC_SOURCE,
                    jsonify(provenance),
                ),
            )
            # Grab the inserted id portably (SQLite autoincrement).
            execute(
                cur,
                "SELECT heuristic_id FROM heuristics "
                "WHERE tenant_id = ? AND source = ? "
                "ORDER BY heuristic_id DESC LIMIT 1",
                (tenant_id, HEURISTIC_SOURCE),
            )
            row = cur.fetchone()
            heuristic_id = int(row[0]) if row else 0

            execute(
                cur,
                "UPDATE proposed_rules SET status = 'approved' "
                "WHERE id = ? AND tenant_id = ?",
                (rule["id"], tenant_id),
            )
            execute(
                cur,
                "UPDATE mentoring_queue SET status = 'resolved' "
                "WHERE id = ? AND tenant_id = ?",
                (queue_id, tenant_id),
            )
        conn.commit()
    return heuristic_id


def _reject(*, tenant_id: str, rule_id: str, queue_id: int) -> None:
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "UPDATE proposed_rules SET status = 'rejected' "
                "WHERE id = ? AND tenant_id = ?",
                (rule_id, tenant_id),
            )
            execute(
                cur,
                "UPDATE mentoring_queue SET status = 'dismissed' "
                "WHERE id = ? AND tenant_id = ?",
                (queue_id, tenant_id),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# UI helpers (printable strings, no Rich markup — tests assert on content)
# ---------------------------------------------------------------------------


def _format_proposal(rule: Dict[str, Any], queue_priority: int) -> str:
    excerpt = (rule.get("verbatim_excerpt") or "").strip()
    if len(excerpt) > 240:
        excerpt = excerpt[:237] + "..."
    return (
        "─" * 60 + "\n"
        f"domain:      {rule.get('domain')}\n"
        f"statement:   {rule.get('proposed_statement')}\n"
        f"excerpt:     {excerpt}\n"
        f"source:      {rule.get('source_path')}\n"
        f"confidence:  {rule.get('confidence_hint')}\n"
        f"priority:    {queue_priority}"
    )


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


def run_review(
    *,
    tenant_id: Optional[str] = None,
    input_fn: Optional[Callable[[str], str]] = None,
    output_fn: Callable[[str], None] = print,
) -> ReviewSummary:
    """Walk the queue once. Returns a ``ReviewSummary``.

    Pure-function shape (input/output injection) so tests can drive it
    deterministically without touching real stdin. ``input_fn=None``
    means "look up ``builtins.input`` at call time" so test monkeypatches
    against ``builtins.input`` (which CLI tests use) still bite.
    """
    if input_fn is None:
        import builtins as _builtins
        input_fn = _builtins.input
    tid = tenant_id or _default_tenant()
    summary = ReviewSummary()

    queue = _list_queue(tid)
    if not queue:
        output_fn("Inbox zero — no queued mentoring items.")
        summary.inbox_zero = True
        return summary

    # Count how many corpus_rule_proposal items we'll actually walk.
    corpus_items = [q for q in queue if q.get("source") == CORPUS_RULE_SOURCE]
    if not corpus_items:
        for q in queue:
            src = q.get("source") or "unknown"
            if src != CORPUS_RULE_SOURCE:
                summary.other_sources_skipped += 1
                summary.handled_sources.append(src)
                output_fn(
                    f"  (skipping {src} item id={q.get('id')} — "
                    "out of scope for this review CLI)"
                )
        output_fn("No corpus_rule_proposal items queued.")
        return summary

    output_fn(
        f"Mentoring review: {len(corpus_items)} corpus rule proposal(s) queued."
    )

    for q in queue:
        source = q.get("source")
        if source != CORPUS_RULE_SOURCE:
            summary.other_sources_skipped += 1
            summary.handled_sources.append(source or "unknown")
            output_fn(
                f"  (skipping {source} item id={q.get('id')} — "
                "out of scope for this review CLI)"
            )
            continue

        payload = q.get("payload") or {}
        rule_id = payload.get("proposed_rule_id")
        rule = _get_proposed_rule(tid, rule_id) if rule_id else None
        if rule is None:
            output_fn(
                f"  (queue item id={q.get('id')} references missing "
                f"proposed_rule_id={rule_id}; skipping)"
            )
            summary.skipped += 1
            continue
        if rule.get("status") != "queued":
            # Someone already acted on the underlying rule out-of-band.
            # Don't double-write. Just close the queue row to 'dismissed'.
            with get_conn() as conn:
                with cursor(conn) as cur:
                    execute(
                        cur,
                        "UPDATE mentoring_queue SET status = 'dismissed' "
                        "WHERE id = ? AND tenant_id = ?",
                        (q["id"], tid),
                    )
                conn.commit()
            summary.skipped += 1
            continue

        output_fn(_format_proposal(rule, q.get("priority") or 4))
        try:
            choice = input_fn("[a]pprove / [r]eject / [e]dit / [s]kip / [q]uit > ")
        except (EOFError, KeyboardInterrupt):
            output_fn("")
            summary.quit = True
            return summary
        choice = (choice or "").strip().lower()

        if choice == "q":
            summary.quit = True
            output_fn("Quit. Remaining items left queued.")
            return summary
        if choice == "s":
            summary.skipped += 1
            continue
        if choice == "a":
            hid = _approve(tenant_id=tid, rule=rule, queue_id=int(q["id"]))
            summary.approved += 1
            output_fn(f"  approved → heuristic_id={hid}")
            continue
        if choice == "r":
            _reject(tenant_id=tid, rule_id=rule["id"], queue_id=int(q["id"]))
            summary.rejected += 1
            output_fn("  rejected.")
            continue
        if choice == "e":
            current = rule.get("proposed_statement") or ""
            output_fn(f"  current statement: {current}")
            try:
                new_text = input_fn("  new statement (blank = keep current) > ")
            except (EOFError, KeyboardInterrupt):
                output_fn("")
                summary.quit = True
                return summary
            new_text = (new_text or "").strip() or current
            hid = _approve(
                tenant_id=tid,
                rule=rule,
                queue_id=int(q["id"]),
                statement_override=new_text,
            )
            summary.edited += 1
            output_fn(f"  edited + approved → heuristic_id={hid}")
            continue

        # Anything else — treat as skip but say so.
        output_fn(f"  (unrecognised choice {choice!r}; skipping)")
        summary.skipped += 1

    output_fn(
        f"\nDone. approved={summary.approved} rejected={summary.rejected} "
        f"edited={summary.edited} skipped={summary.skipped}"
    )
    return summary


# ---------------------------------------------------------------------------
# CLI entrypoint (called from solomon.cli)
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    """``solomon mentoring review`` entrypoint.

    Reads from real stdin/stdout. Returns 0 on success (including
    inbox-zero and clean quit), 1 on storage failure.
    """
    _ = argv  # currently no flags
    try:
        run_review()
    except Exception as e:  # noqa: BLE001
        print(f"mentoring review: {e}", file=sys.stderr)
        return 1
    return 0

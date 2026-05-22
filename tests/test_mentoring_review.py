"""Tests for solomon.mentoring.review — the corpus rule proposal review loop.

The loop reads ``mentoring_queue`` (priority ASC, surfaced_at ASC) and
walks each ``corpus_rule_proposal`` past the owner. Stdin is injected
via ``input_fn=`` so we can script answers deterministically (same
pattern as tests/test_session_runner.py).

Each test seeds the DB with crafted rows (no LLM calls, no embeddings)
and asserts on the post-state plus the returned ``ReviewSummary``.
"""

from __future__ import annotations

from typing import List, Optional

import pytest

from solomon.mentoring import review as mr
from solomon.storage.pool import cursor, execute, get_conn, jsonify


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _scripted(answers):
    """Build an input_fn that returns the next item per call."""
    it = iter(answers)

    def _inner(prompt):
        try:
            return next(it)
        except StopIteration:
            # Treat exhaustion like Ctrl-D so the loop exits cleanly.
            raise EOFError("scripted input exhausted")

    return _inner


def _seed_rule(
    *,
    rule_id: str,
    domain: str = "pricing",
    statement: str = "Always quote in CAD.",
    verbatim: str = "We always quote in CAD.",
    source_path: str = "corpus/inbox/policy.txt",
    priority: int = 4,
    surfaced_at: str = "2026-05-23T10:00:00Z",
    queue_source: str = mr.CORPUS_RULE_SOURCE,
    tenant_id: str = "default",
    confidence_hint: str = "stated",
):
    """Seed one proposed_rules row plus one matching mentoring_queue row.

    Returns the queue row's integer id.
    """
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO proposed_rules "
                "(id, tenant_id, domain, proposed_statement, "
                " verbatim_excerpt, source_path, keywords, "
                " confidence_hint, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                (
                    rule_id, tenant_id, domain, statement, verbatim,
                    source_path, jsonify([]), confidence_hint,
                    surfaced_at,
                ),
            )
            payload = {
                "proposed_rule_id": rule_id,
                "domain": domain,
                "source_path": source_path,
                "proposed_statement": statement,
                "verbatim_excerpt": verbatim,
                "confidence_hint": confidence_hint,
            }
            execute(
                cur,
                "INSERT INTO mentoring_queue "
                "(tenant_id, source, surfaced_at, status, priority, payload) "
                "VALUES (?, ?, ?, 'queued', ?, ?)",
                (
                    tenant_id, queue_source, surfaced_at,
                    priority, jsonify(payload),
                ),
            )
            execute(
                cur,
                "SELECT id FROM mentoring_queue "
                "WHERE tenant_id = ? ORDER BY id DESC LIMIT 1",
                (tenant_id,),
            )
            row = cur.fetchone()
        conn.commit()
    return int(row[0])


def _seed_non_corpus_queue_row(
    *,
    source: str = "contradiction",
    priority: int = 3,
    surfaced_at: str = "2026-05-23T09:00:00Z",
    tenant_id: str = "default",
):
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO mentoring_queue "
                "(tenant_id, source, surfaced_at, status, priority, payload) "
                "VALUES (?, ?, ?, 'queued', ?, ?)",
                (tenant_id, source, surfaced_at, priority,
                 jsonify({"note": "out of scope for review CLI"})),
            )
        conn.commit()


def _rule_status(rule_id: str) -> Optional[str]:
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT status FROM proposed_rules WHERE id = ?",
                (rule_id,),
            )
            row = cur.fetchone()
    return row[0] if row else None


def _queue_status(queue_id: int) -> Optional[str]:
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT status FROM mentoring_queue WHERE id = ?",
                (queue_id,),
            )
            row = cur.fetchone()
    return row[0] if row else None


def _count_heuristics(tenant_id: str = "default") -> int:
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT COUNT(*) FROM heuristics WHERE tenant_id = ?",
                (tenant_id,),
            )
            row = cur.fetchone()
    return int(row[0]) if row else 0


def _latest_heuristic(tenant_id: str = "default"):
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT heuristic_id, scope, domain, condition, action, "
                "       confidence, source, status "
                "FROM heuristics "
                "WHERE tenant_id = ? "
                "ORDER BY heuristic_id DESC LIMIT 1",
                (tenant_id,),
            )
            row = cur.fetchone()
    return row


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_empty_queue_says_inbox_zero(solomon_db):
    outputs: List[str] = []
    summary = mr.run_review(
        input_fn=lambda p: "q",
        output_fn=lambda s: outputs.append(s),
    )
    assert summary.inbox_zero is True
    assert summary.approved == 0 and summary.rejected == 0
    assert any("Inbox zero" in line for line in outputs)


def test_approve_promotes_to_heuristics_and_flips_statuses(solomon_db):
    qid = _seed_rule(rule_id="r1")
    before = _count_heuristics()
    summary = mr.run_review(
        input_fn=_scripted(["a"]),
        output_fn=lambda _s: None,
    )
    assert summary.approved == 1
    assert _rule_status("r1") == "approved"
    assert _queue_status(qid) == "resolved"
    assert _count_heuristics() == before + 1
    h = _latest_heuristic()
    # heuristic_id, scope, domain, condition, action, confidence, source, status
    assert h is not None
    assert h[1] == mr.HEURISTIC_SCOPE_DEFAULT
    assert h[2] == "pricing"
    assert h[4] == "Always quote in CAD."  # action == proposed_statement
    assert h[5] == mr.HEURISTIC_INITIAL_CONFIDENCE
    assert h[6] == mr.HEURISTIC_SOURCE
    assert h[7] == "active"


def test_reject_flips_statuses_and_writes_no_heuristic(solomon_db):
    qid = _seed_rule(rule_id="r1")
    before = _count_heuristics()
    summary = mr.run_review(
        input_fn=_scripted(["r"]),
        output_fn=lambda _s: None,
    )
    assert summary.rejected == 1
    assert _rule_status("r1") == "rejected"
    assert _queue_status(qid) == "dismissed"
    assert _count_heuristics() == before  # nothing inserted


def test_edit_prompts_for_new_text_then_approves_with_edit(solomon_db):
    qid = _seed_rule(rule_id="r1", statement="Quote in CAD.")
    summary = mr.run_review(
        input_fn=_scripted(["e", "Always quote prices in CAD, never USD."]),
        output_fn=lambda _s: None,
    )
    assert summary.edited == 1
    assert summary.approved == 0
    assert _rule_status("r1") == "approved"
    assert _queue_status(qid) == "resolved"
    h = _latest_heuristic()
    assert h is not None
    assert h[4] == "Always quote prices in CAD, never USD."


def test_edit_with_blank_keeps_existing_statement(solomon_db):
    _seed_rule(rule_id="r1", statement="Original statement.")
    summary = mr.run_review(
        input_fn=_scripted(["e", "   "]),
        output_fn=lambda _s: None,
    )
    assert summary.edited == 1
    h = _latest_heuristic()
    assert h is not None
    assert h[4] == "Original statement."


def test_skip_leaves_rows_untouched(solomon_db):
    qid = _seed_rule(rule_id="r1")
    before = _count_heuristics()
    summary = mr.run_review(
        input_fn=_scripted(["s"]),
        output_fn=lambda _s: None,
    )
    assert summary.skipped == 1
    assert _rule_status("r1") == "queued"
    assert _queue_status(qid) == "queued"
    assert _count_heuristics() == before


def test_quit_exits_immediately_without_touching_remaining_items(solomon_db):
    qid1 = _seed_rule(rule_id="r1", surfaced_at="2026-05-23T10:00:00Z")
    qid2 = _seed_rule(
        rule_id="r2", surfaced_at="2026-05-23T10:05:00Z",
        statement="Second rule.", verbatim="Second.",
    )
    summary = mr.run_review(
        input_fn=_scripted(["q"]),
        output_fn=lambda _s: None,
    )
    assert summary.quit is True
    assert summary.approved == 0 and summary.rejected == 0
    # Both still queued.
    assert _rule_status("r1") == "queued"
    assert _rule_status("r2") == "queued"
    assert _queue_status(qid1) == "queued"
    assert _queue_status(qid2) == "queued"


def test_non_corpus_source_is_skipped_with_one_line_note(solomon_db):
    _seed_non_corpus_queue_row(source="contradiction", priority=3)
    _seed_non_corpus_queue_row(source="drift", priority=5)

    lines: List[str] = []
    summary = mr.run_review(
        input_fn=_scripted([]),
        output_fn=lambda s: lines.append(s),
    )
    assert summary.other_sources_skipped == 2
    # Sources we saw and silently skipped
    assert "contradiction" in summary.handled_sources
    assert "drift" in summary.handled_sources
    # Each non-corpus row produced exactly one note line
    notes = [line for line in lines if "out of scope" in line]
    assert len(notes) == 2


def test_priority_ordering_is_respected(solomon_db):
    """Priority 2 should be presented before priority 4 regardless of
    surfaced_at."""
    qid_low_prio = _seed_rule(
        rule_id="r_late_but_urgent",
        priority=2,
        surfaced_at="2026-05-23T11:00:00Z",
        statement="Urgent rule.",
        verbatim="Urgent verbatim.",
        source_path="corpus/inbox/urgent.txt",
    )
    qid_normal = _seed_rule(
        rule_id="r_early_normal",
        priority=4,
        surfaced_at="2026-05-23T09:00:00Z",
        statement="Normal rule.",
        verbatim="Normal verbatim.",
        source_path="corpus/inbox/normal.txt",
    )

    seen_order: List[str] = []

    def _grab(prompt):
        # Output formatter wrote the statement to its own line; we sniff
        # which one is being asked about by reading the last lines.
        return "s"  # skip everything but record what we saw

    lines: List[str] = []
    mr.run_review(
        input_fn=_grab,
        output_fn=lambda s: lines.append(s),
    )

    # Index where each rule's panel first appears.
    joined = "\n".join(lines)
    idx_urgent = joined.find("Urgent rule.")
    idx_normal = joined.find("Normal rule.")
    assert idx_urgent != -1 and idx_normal != -1
    assert idx_urgent < idx_normal, (
        "priority=2 item should be surfaced before priority=4 item"
    )
    # Both still queued because the test answered 's'.
    assert _queue_status(qid_low_prio) == "queued"
    assert _queue_status(qid_normal) == "queued"


def test_missing_proposed_rule_is_skipped_gracefully(solomon_db):
    """A queue row whose proposed_rule_id no longer exists is just skipped."""
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO mentoring_queue "
                "(tenant_id, source, surfaced_at, status, priority, payload) "
                "VALUES (?, ?, ?, 'queued', ?, ?)",
                (
                    "default", mr.CORPUS_RULE_SOURCE,
                    "2026-05-23T10:00:00Z", 4,
                    jsonify({"proposed_rule_id": "does-not-exist"}),
                ),
            )
        conn.commit()
    summary = mr.run_review(
        input_fn=_scripted([]),
        output_fn=lambda _s: None,
    )
    assert summary.skipped == 1
    assert summary.approved == 0


def test_already_approved_rule_closes_dangling_queue_row(solomon_db):
    """If proposed_rules.status was flipped out-of-band, the queue row gets
    closed to 'dismissed' (not double-written)."""
    qid = _seed_rule(rule_id="r_ext")
    # Out-of-band: flip the rule to approved directly.
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "UPDATE proposed_rules SET status = 'approved' WHERE id = ?",
                ("r_ext",),
            )
        conn.commit()
    before = _count_heuristics()
    summary = mr.run_review(
        input_fn=_scripted([]),
        output_fn=lambda _s: None,
    )
    assert summary.skipped == 1
    assert summary.approved == 0
    assert _queue_status(qid) == "dismissed"
    assert _count_heuristics() == before

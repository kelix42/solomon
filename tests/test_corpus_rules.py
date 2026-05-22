"""Tests for solomon.corpus.rules — THE CRITICAL FILE.

This module is the bridge that lets bulk material teach Solomon the
same rules the interview engine extracts, but only after the owner
reviews them. Idempotency + dedup + paired mentoring_queue rows are
non-negotiable.
"""

from __future__ import annotations

import json

import pytest

from solomon.corpus import rules as r
from solomon.storage.pool import cursor, execute, get_conn


def _mentoring_queue_rows(tenant_id="default"):
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT id, source, status, priority, payload "
                "FROM mentoring_queue WHERE tenant_id = ? ORDER BY id",
                (tenant_id,),
            )
            rows = cur.fetchall()
    out = []
    for row in rows:
        keys = ["id", "source", "status", "priority", "payload"]
        if hasattr(row, "keys"):
            d = {k: row[k] for k in keys if k in row.keys()}
        else:
            d = dict(zip(keys, row))
        try:
            d["payload"] = json.loads(d["payload"])
        except Exception:
            pass
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# dedup_key
# ---------------------------------------------------------------------------


def test_dedup_key_deterministic():
    a = r.dedup_key(source_path="x.txt", verbatim_excerpt="we never quote below cost+15%")
    b = r.dedup_key(source_path="x.txt", verbatim_excerpt="we never quote below cost+15%")
    assert a == b


def test_dedup_key_differs_for_different_sources():
    a = r.dedup_key(source_path="x.txt", verbatim_excerpt="we never quote below cost+15%")
    b = r.dedup_key(source_path="y.txt", verbatim_excerpt="we never quote below cost+15%")
    assert a != b


# ---------------------------------------------------------------------------
# Write — happy path
# ---------------------------------------------------------------------------


def _good_proposal(**overrides):
    base = {
        "domain": "pricing",
        "proposed_statement": "Never quote below cost+15% on commercial jobs.",
        "verbatim_excerpt": "we never quote below cost+15%",
        "keywords": ["margin", "commercial"],
        "confidence_hint": "stated",
    }
    base.update(overrides)
    return base


def test_write_inserts_proposed_rule_and_queue_row(solomon_db):
    n = r.write_proposed_rules(
        proposals=[_good_proposal()],
        source_path="corpus/raw/docs/quote-policy.txt",
    )
    assert n == 1

    queued = r.list_queued()
    assert len(queued) == 1
    pr = queued[0]
    assert pr["domain"] == "pricing"
    assert pr["proposed_statement"].startswith("Never quote")
    assert pr["confidence_hint"] == "stated"
    assert pr["keywords"] == ["margin", "commercial"]

    mq = _mentoring_queue_rows()
    assert len(mq) == 1
    assert mq[0]["source"] == "corpus_rule_proposal"
    assert mq[0]["priority"] == 4
    assert mq[0]["status"] == "queued"
    payload = mq[0]["payload"]
    assert payload["proposed_rule_id"] == pr["id"]
    assert payload["domain"] == "pricing"
    assert payload["source_path"] == "corpus/raw/docs/quote-policy.txt"


def test_write_multiple_proposals(solomon_db):
    proposals = [
        _good_proposal(verbatim_excerpt="rule A excerpt"),
        _good_proposal(domain="ops", verbatim_excerpt="rule B excerpt"),
        _good_proposal(domain="hiring", verbatim_excerpt="rule C excerpt"),
    ]
    n = r.write_proposed_rules(proposals=proposals, source_path="src.txt")
    assert n == 3
    assert len(r.list_queued()) == 3
    assert len(_mentoring_queue_rows()) == 3


# ---------------------------------------------------------------------------
# Idempotency / dedup
# ---------------------------------------------------------------------------


def test_dedup_skips_same_excerpt_same_source(solomon_db):
    p = _good_proposal()
    assert r.write_proposed_rules(proposals=[p], source_path="src.txt") == 1
    # Same excerpt + same source → skipped.
    assert r.write_proposed_rules(proposals=[p], source_path="src.txt") == 0
    assert len(r.list_queued()) == 1
    assert len(_mentoring_queue_rows()) == 1


def test_same_excerpt_different_source_allowed(solomon_db):
    p = _good_proposal()
    assert r.write_proposed_rules(proposals=[p], source_path="a.txt") == 1
    assert r.write_proposed_rules(proposals=[p], source_path="b.txt") == 1
    assert len(r.list_queued()) == 2


# ---------------------------------------------------------------------------
# Validation — bad proposals are silently skipped
# ---------------------------------------------------------------------------


def test_skip_missing_verbatim(solomon_db):
    n = r.write_proposed_rules(
        proposals=[_good_proposal(verbatim_excerpt="")],
        source_path="src.txt",
    )
    assert n == 0
    assert r.list_queued() == []


def test_skip_missing_statement(solomon_db):
    n = r.write_proposed_rules(
        proposals=[_good_proposal(proposed_statement="")],
        source_path="src.txt",
    )
    assert n == 0


def test_skip_invalid_domain(solomon_db):
    n = r.write_proposed_rules(
        proposals=[_good_proposal(domain="not-a-real-domain")],
        source_path="src.txt",
    )
    assert n == 0


def test_unknown_confidence_clamped_to_stated(solomon_db):
    r.write_proposed_rules(
        proposals=[_good_proposal(confidence_hint="confident!!")],
        source_path="src.txt",
    )
    pr = r.list_queued()[0]
    assert pr["confidence_hint"] == "stated"


def test_keywords_normalised(solomon_db):
    r.write_proposed_rules(
        proposals=[_good_proposal(keywords=[" Margin ", "Commercial", "", 7])],
        source_path="src.txt",
    )
    pr = r.list_queued()[0]
    assert pr["keywords"] == ["margin", "commercial"]


def test_empty_proposals_returns_zero(solomon_db):
    assert r.write_proposed_rules(proposals=[], source_path="x") == 0


def test_mixed_good_and_bad(solomon_db):
    proposals = [
        _good_proposal(verbatim_excerpt="good"),
        _good_proposal(domain="bad"),
        _good_proposal(verbatim_excerpt="also good", proposed_statement="another rule"),
    ]
    n = r.write_proposed_rules(proposals=proposals, source_path="src.txt")
    assert n == 2
    assert len(r.list_queued()) == 2


# ---------------------------------------------------------------------------
# Status transitions + forget cascade
# ---------------------------------------------------------------------------


def test_mark_dismissed_removes_from_queued(solomon_db):
    r.write_proposed_rules(proposals=[_good_proposal()], source_path="s")
    pr_id = r.list_queued()[0]["id"]
    r.mark_dismissed(pr_id)
    assert r.list_queued() == []


def test_delete_for_source_cascades(solomon_db):
    p1 = _good_proposal(verbatim_excerpt="rule A")
    p2 = _good_proposal(verbatim_excerpt="rule B")
    p3 = _good_proposal(verbatim_excerpt="rule C")
    r.write_proposed_rules(proposals=[p1, p2], source_path="src.txt")
    r.write_proposed_rules(proposals=[p3], source_path="other.txt")
    assert len(r.list_queued()) == 3
    assert len(_mentoring_queue_rows()) == 3

    n = r.delete_for_source("src.txt")
    assert n == 2

    remaining = r.list_queued()
    assert len(remaining) == 1
    assert remaining[0]["source_path"] == "other.txt"
    mq = _mentoring_queue_rows()
    assert len(mq) == 1


# ---------------------------------------------------------------------------
# Source path semantics
# ---------------------------------------------------------------------------


def test_payload_carries_source_path_for_reviewer(solomon_db):
    r.write_proposed_rules(
        proposals=[_good_proposal()],
        source_path="corpus/raw/docs/quote-policy.txt",
    )
    mq = _mentoring_queue_rows()[0]
    assert mq["payload"]["source_path"] == "corpus/raw/docs/quote-policy.txt"
    assert mq["payload"]["verbatim_excerpt"] == "we never quote below cost+15%"

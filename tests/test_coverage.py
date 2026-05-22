"""Tests for solomon.onboarding.interview.coverage.

The coverage module is pure SQL plus the gap-score arithmetic helper.
Tests:
  - gap_score_for arithmetic matches the documented formula.
  - next_sub_topic returns the row with the highest gap_score > 0.4.
  - refresh() bumps turns_since_last_capture only when no items landed.
  - is_session_complete() implements the dual saturation rule.
  - required_field_gaps() detects which field tags are not yet captured.
"""

import pytest

from solomon.onboarding.interview import coverage
from solomon.storage import pool


# ---------------------------------------------------------------------------
# Pure formula (no DB)
# ---------------------------------------------------------------------------

def test_gap_score_unprobed_is_one():
    assert coverage.gap_score_for(probes_asked=0, items_captured=0) == 1.0


def test_gap_score_decays_with_captures():
    # Two captures at probes_asked=4 → score = 1 - 2 * (1/5) = 0.6
    score = coverage.gap_score_for(probes_asked=4, items_captured=2)
    assert abs(score - 0.6) < 1e-9


def test_gap_score_floors_at_zero():
    # Pathological: many captures at low probes_asked → would go negative.
    assert coverage.gap_score_for(probes_asked=1, items_captured=10) == 0.0


def test_gap_score_negative_inputs_return_one():
    assert coverage.gap_score_for(probes_asked=-1, items_captured=0) == 1.0
    assert coverage.gap_score_for(probes_asked=0, items_captured=-1) == 1.0


# ---------------------------------------------------------------------------
# DB-backed helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def open_session(solomon_db):
    session_id = "test-coverage-session"
    with pool.get_conn() as conn:
        with pool.cursor(conn) as cur:
            pool.execute(
                cur,
                "INSERT INTO sessions (session_id, tenant_id, domain, mode, status) "
                "VALUES (?, 'default', 'test_domain', 'onboarding', 'open')",
                (session_id,),
            )
        conn.commit()
    return session_id


def _insert_coverage(session_id, sub_topic, *, probes=0, captured=0, gap=1.0,
                    turns_dry=0):
    with pool.get_conn() as conn:
        with pool.cursor(conn) as cur:
            pool.execute(
                cur,
                "INSERT INTO coverage (tenant_id, session_id, domain, sub_topic, "
                "probes_asked, items_captured, gap_score, "
                "turns_since_last_capture) VALUES "
                "('default', ?, 'test_domain', ?, ?, ?, ?, ?)",
                (session_id, sub_topic, probes, captured, gap, turns_dry),
            )
        conn.commit()


def test_next_sub_topic_picks_highest_gap_above_threshold(solomon_db, open_session):
    _insert_coverage(open_session, "saturated", probes=8, captured=5, gap=0.1)
    _insert_coverage(open_session, "high_gap", probes=1, captured=0, gap=0.9)
    _insert_coverage(open_session, "medium_gap", probes=2, captured=1, gap=0.5)
    assert coverage.next_sub_topic(open_session, "test_domain") == "high_gap"


def test_next_sub_topic_returns_none_when_all_saturated(solomon_db, open_session):
    _insert_coverage(open_session, "a", gap=0.2)
    _insert_coverage(open_session, "b", gap=0.3)
    assert coverage.next_sub_topic(open_session, "test_domain") is None


def test_next_sub_topic_empty_session_returns_none(solomon_db, open_session):
    assert coverage.next_sub_topic(open_session, "test_domain") is None


# ---------------------------------------------------------------------------
# refresh() — per-turn maintenance
# ---------------------------------------------------------------------------

def test_refresh_with_no_captures_bumps_turns_dry(solomon_db, open_session):
    _insert_coverage(open_session, "a", probes=2, captured=0, gap=1.0, turns_dry=3)
    _insert_coverage(open_session, "b", probes=1, captured=0, gap=1.0, turns_dry=0)
    coverage.refresh(open_session, "test_domain", captured_count_delta=0)
    with pool.get_conn() as conn:
        with pool.cursor(conn) as cur:
            pool.execute(
                cur,
                "SELECT sub_topic, turns_since_last_capture FROM coverage "
                "WHERE session_id=? AND domain=? ORDER BY sub_topic",
                (open_session, "test_domain"),
            )
            rows = list(cur.fetchall())
    by_topic = {r[0]: int(r[1]) for r in rows}
    assert by_topic == {"a": 4, "b": 1}


def test_refresh_with_captures_is_a_noop(solomon_db, open_session):
    """When extraction landed rows, the counter is already 0'd inside
    extraction._bump_coverage_capture; refresh() must not double-count."""
    _insert_coverage(open_session, "a", probes=2, captured=0, gap=1.0, turns_dry=3)
    coverage.refresh(open_session, "test_domain", captured_count_delta=2)
    with pool.get_conn() as conn:
        with pool.cursor(conn) as cur:
            pool.execute(
                cur,
                "SELECT turns_since_last_capture FROM coverage "
                "WHERE session_id=? AND sub_topic='a'",
                (open_session,),
            )
            row = cur.fetchone()
    assert int(row[0]) == 3  # unchanged


# ---------------------------------------------------------------------------
# is_session_complete — dual rule
# ---------------------------------------------------------------------------

def test_is_session_complete_empty_returns_false(solomon_db, open_session):
    """No coverage rows yet — we haven't even started."""
    assert coverage.is_session_complete(open_session, "test_domain") is False


def test_is_session_complete_all_saturated_returns_true(solomon_db, open_session):
    """Dual rule branch (a): every sub_topic has items_captured >= 1 AND gap < 0.4."""
    _insert_coverage(open_session, "a", probes=5, captured=3, gap=0.3, turns_dry=1)
    _insert_coverage(open_session, "b", probes=6, captured=2, gap=0.2, turns_dry=0)
    assert coverage.is_session_complete(open_session, "test_domain") is True


def test_is_session_complete_diminishing_returns(solomon_db, open_session):
    """Dual rule branch (b): max turns_since_last_capture > 5 forces close."""
    _insert_coverage(open_session, "a", probes=1, captured=0, gap=0.9, turns_dry=6)
    _insert_coverage(open_session, "b", probes=1, captured=0, gap=0.8, turns_dry=2)
    assert coverage.is_session_complete(open_session, "test_domain") is True


def test_is_session_complete_partial_is_false(solomon_db, open_session):
    """Some sub_topics saturated, others not, dry counter low → not done yet."""
    _insert_coverage(open_session, "a", probes=5, captured=3, gap=0.3, turns_dry=1)
    _insert_coverage(open_session, "b", probes=1, captured=0, gap=0.9, turns_dry=1)
    assert coverage.is_session_complete(open_session, "test_domain") is False


# ---------------------------------------------------------------------------
# required_field_gaps
# ---------------------------------------------------------------------------

def _insert_captured(session_id, item_id, *, keywords=None):
    import json
    kw = json.dumps(keywords or [])
    with pool.get_conn() as conn:
        with pool.cursor(conn) as cur:
            pool.execute(
                cur,
                "INSERT INTO captured_items (id, tenant_id, session_id, domain, "
                "type, statement, verbatim_phrase, keywords) "
                "VALUES (?, 'default', ?, 'test_domain', 'preference', ?, ?, ?)",
                (item_id, session_id, f"stmt {item_id}", f"verb {item_id}", kw),
            )
        conn.commit()


def test_required_field_gaps_finds_unfilled(solomon_db, open_session):
    _insert_captured(open_session, "I1", keywords=["field:business_category", "industry"])
    _insert_captured(open_session, "I2", keywords=["channel"])
    gaps = coverage.required_field_gaps(
        open_session,
        ["business_category", "primary_product", "growth_stage"],
    )
    assert gaps == ["primary_product", "growth_stage"]


def test_required_field_gaps_all_filled_returns_empty(solomon_db, open_session):
    _insert_captured(open_session, "I1", keywords=["field:a"])
    _insert_captured(open_session, "I2", keywords=["field:b"])
    assert coverage.required_field_gaps(open_session, ["a", "b"]) == []


def test_required_field_gaps_empty_list_returns_empty(solomon_db, open_session):
    assert coverage.required_field_gaps(open_session, []) == []

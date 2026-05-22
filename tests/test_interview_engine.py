"""Tests for solomon.onboarding.interview.engine.select_next_probe.

The engine is the only piece of the interview pipeline that makes
zero LLM calls — pure SQL + YAML + verbatim string substitution.
That makes it easy to test deterministically.

Covered:
  - Pending clarification rows jump the queue (verbatim).
  - Keyword match returns the highest-priority template with {phrase}
    substituted by an echo from the owner's last answer.
  - Saturated sub_topics (gap_score < 0.4 AND probes_asked >= 5) are
    skipped.
  - Fallback prompts kick in when nothing keys.
  - Empty owner text falls back to a domain or generic fallback (never
    raises and never returns an empty string).
  - Probe-asked recording bumps the coverage row.
"""

import pytest
import yaml

from solomon.onboarding.interview import engine
from solomon.storage import pool


@pytest.fixture(autouse=True)
def _clear_engine_cache():
    """The engine caches YAML libraries module-globally; clear per-test
    so each test sees the fresh disk file."""
    engine._LIBRARY_CACHE.clear()
    yield
    engine._LIBRARY_CACHE.clear()


@pytest.fixture
def domain_library(monkeypatch, tmp_path):
    """Write a tiny probe library to disk and point the engine at it."""
    lib_dir = tmp_path / "probe_library"
    lib_dir.mkdir()
    monkeypatch.setattr(engine, "PROBE_LIBRARY_DIR", lib_dir)

    test_yaml = {
        "domain": "test_domain",
        "version": "0.1.0",
        "priority": 5,
        "keywords": {
            "customer": [
                {"priority": 2, "template": "{phrase}. Walk me through the last one."},
                {"priority": 1, "template": "{phrase}. Who was that?"},
            ],
            "pricing": [
                {"priority": 1, "template": "{phrase}. Where does the market expect you to sit?"},
            ],
        },
        "fallbacks": ["What's the rule you would give a new hire on day one?"],
    }
    (lib_dir / "test_domain.yaml").write_text(yaml.safe_dump(test_yaml))

    generic_yaml = {
        "domain": "_generic",
        "version": "0.1.0",
        "priority": 1,
        "keywords": {},
        "fallbacks": ["GENERIC_FALLBACK_PROMPT"],
    }
    (lib_dir / "_generic.yaml").write_text(yaml.safe_dump(generic_yaml))

    return lib_dir


@pytest.fixture
def open_session(solomon_db):
    """Create one open session row so coverage upserts have a tenant FK."""
    session_id = "test-session-1"
    with pool.get_conn() as conn:
        with pool.cursor(conn) as cur:
            pool.execute(
                cur,
                "INSERT INTO sessions (session_id, tenant_id, domain, mode, status) "
                "VALUES (?, ?, ?, 'onboarding', 'open')",
                (session_id, "default", "test_domain"),
            )
        conn.commit()
    return session_id


# ---------------------------------------------------------------------------
# Pending clarification (priority 1)
# ---------------------------------------------------------------------------

def test_pending_clarification_jumps_the_queue(solomon_db, domain_library, open_session):
    """A row in clarification_queue.status='pending' must be asked verbatim."""
    # Seed a captured_items row pair the clarification can reference.
    with pool.get_conn() as conn:
        with pool.cursor(conn) as cur:
            for iid in ("ITEM_A", "ITEM_B"):
                pool.execute(
                    cur,
                    "INSERT INTO captured_items (id, tenant_id, session_id, "
                    "domain, type, statement, verbatim_phrase) "
                    "VALUES (?, 'default', ?, 'test_domain', 'preference', ?, ?)",
                    (iid, open_session, f"stmt {iid}", f"verb {iid}"),
                )
            pool.execute(
                cur,
                "INSERT INTO clarification_queue (tenant_id, session_id, "
                "new_item_id, conflicting_item_id, reason, status) "
                "VALUES ('default', ?, 'ITEM_B', 'ITEM_A', ?, 'pending')",
                (open_session, "Earlier you said X; just now Y. Which wins?"),
            )
        conn.commit()

    probe = engine.select_next_probe(open_session, "test_domain", "anything at all")
    assert probe == "Earlier you said X; just now Y. Which wins?"


# ---------------------------------------------------------------------------
# Keyword match + {phrase} substitution
# ---------------------------------------------------------------------------

def test_keyword_match_uses_lowest_priority_template(solomon_db, domain_library, open_session):
    """Lower priority number wins (matches Drive's `probe_priority` rule)."""
    probe = engine.select_next_probe(
        open_session, "test_domain",
        "Most of our business is with one big customer this quarter.",
    )
    # priority=1 template for `customer` is "{phrase}. Who was that?"
    assert "Who was that?" in probe
    # The phrase echo should come from the owner's text, not invented.
    echo = probe.split(". Who was that?")[0]
    assert echo in "Most of our business is with one big customer this quarter."


def test_keyword_match_strips_trailing_punctuation_from_phrase(
    solomon_db, domain_library, open_session
):
    """The {phrase} renderer strips trailing .!?, so probes read naturally."""
    probe = engine.select_next_probe(
        open_session, "test_domain", "We have one big customer.",
    )
    # Should not become "...big customer.. Who was that?"
    assert ".." not in probe


# ---------------------------------------------------------------------------
# Saturation
# ---------------------------------------------------------------------------

def test_saturated_keyword_is_skipped(solomon_db, domain_library, open_session):
    """A saturated coverage row (gap<0.4 AND probes>=5) must be skipped."""
    # Mark `customer` saturated; `pricing` still wide open.
    with pool.get_conn() as conn:
        with pool.cursor(conn) as cur:
            pool.execute(
                cur,
                "INSERT INTO coverage (tenant_id, session_id, domain, sub_topic, "
                "probes_asked, items_captured, gap_score) "
                "VALUES ('default', ?, 'test_domain', 'customer', 6, 4, 0.2)",
                (open_session,),
            )
        conn.commit()

    probe = engine.select_next_probe(
        open_session, "test_domain",
        "We talk about both pricing and customer mix every day.",
    )
    # Both keywords match, but `customer` is saturated → engine picks `pricing`.
    assert "market expect you to sit" in probe


# ---------------------------------------------------------------------------
# Fallbacks
# ---------------------------------------------------------------------------

def test_fallback_when_no_keyword_matches(solomon_db, domain_library, open_session):
    """No keyword hit → engine returns one of the domain's fallbacks."""
    probe = engine.select_next_probe(
        open_session, "test_domain", "lorem ipsum dolor sit amet",
    )
    assert probe == "What's the rule you would give a new hire on day one?"


def test_empty_last_answer_returns_a_question(solomon_db, domain_library, open_session):
    """First turn of a session has no last_answer; engine must still produce a probe."""
    probe = engine.select_next_probe(open_session, "test_domain", "")
    assert isinstance(probe, str) and probe.strip()


def test_unknown_domain_falls_back_to_generic(solomon_db, domain_library, open_session):
    """Asking about a domain with no library file uses _generic.yaml's fallbacks."""
    probe = engine.select_next_probe(open_session, "nonexistent_domain", "")
    assert probe == "GENERIC_FALLBACK_PROMPT"


# ---------------------------------------------------------------------------
# Coverage side-effects
# ---------------------------------------------------------------------------

def test_select_next_probe_records_probe_asked(solomon_db, domain_library, open_session):
    """After a keyword hit, the matching coverage row bumps probes_asked."""
    engine.select_next_probe(
        open_session, "test_domain", "We sell to one big customer.",
    )
    with pool.get_conn() as conn:
        with pool.cursor(conn) as cur:
            pool.execute(
                cur,
                "SELECT probes_asked, library_version_seen FROM coverage "
                "WHERE session_id=? AND domain=? AND sub_topic=?",
                (open_session, "test_domain", "customer"),
            )
            row = cur.fetchone()
    assert row is not None
    assert int(row[0]) == 1
    assert row[1] == "0.1.0"


# ---------------------------------------------------------------------------
# Template helpers (no DB needed)
# ---------------------------------------------------------------------------

def test_render_template_substitutes_phrase():
    out = engine._render_template("{phrase}. Tell me more.", "the slow quarter")
    assert out == "the slow quarter. Tell me more."


def test_render_template_empty_phrase_strips_leading_echo():
    out = engine._render_template("{phrase}. Tell me more.", "")
    # When the phrase is empty, the leading "{phrase}. " is dropped.
    assert out == "Tell me more."


def test_best_template_picks_lowest_priority_number():
    block = [
        {"priority": 3, "template": "third"},
        {"priority": 1, "template": "first"},
        {"priority": 2, "template": "second"},
    ]
    assert engine._best_template(block) == "first"

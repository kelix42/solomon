"""Tests for solomon.autonomy.ladder — the L0–L4 helpers.

Covers:
  - LEVEL_NAMES mapping
  - owner_state_ceiling(state) for each colour + unknown/None
  - effective_for(scope, owner_state) with mixed inputs (int/str/None)
  - scope_level() reads the scope_autonomy table
  - legacy AUTONOMY_LEVELS tuple is preserved (backwards compat)
"""

from __future__ import annotations

import pytest

from solomon.autonomy import ladder
from solomon.autonomy.ladder import (
    AUTONOMY_LEVELS,
    LEVEL_INTS,
    LEVEL_NAMES,
    effective_for,
    owner_state_ceiling,
    scope_level,
)
from solomon.storage.pool import cursor, execute, get_conn


# ---------------------------------------------------------------------------
# LEVEL_NAMES
# ---------------------------------------------------------------------------

def test_level_names_mapping():
    """All five rungs are present with the canonical Drive names."""
    assert LEVEL_NAMES == {
        0: "manual",
        1: "suggested",
        2: "drafted",
        3: "supervised",
        4: "autonomous",
    }
    assert LEVEL_INTS == (0, 1, 2, 3, 4)


def test_legacy_tuple_preserved():
    """The 4-string AUTONOMY_LEVELS tuple is kept for job_7 + conductor compat."""
    assert AUTONOMY_LEVELS == ("watch", "suggest", "act_with_approval", "act_alone")


# ---------------------------------------------------------------------------
# owner_state_ceiling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "state,expected",
    [
        ("green", 4),
        ("Green", 4),
        ("GREEN", 4),
        ("yellow", 2),
        ("Yellow", 2),
        ("red", 1),
        ("Red", 1),
        ("unknown", 4),
        ("UNKNOWN", 4),
        (None, 4),
        ("", 4),
        ("nonsense", 4),
    ],
)
def test_owner_state_ceiling(state, expected):
    assert owner_state_ceiling(state) == expected


# ---------------------------------------------------------------------------
# effective_for
# ---------------------------------------------------------------------------

def test_effective_for_green_unrestricted():
    """Green state → ceiling 4 → effective == scope_level."""
    for sl in range(5):
        assert effective_for(sl, "green") == sl


def test_effective_for_yellow_caps_at_2():
    """Yellow → ceiling 2 → caps any higher scope at 2."""
    assert effective_for(4, "yellow") == 2
    assert effective_for(3, "yellow") == 2
    assert effective_for(2, "yellow") == 2
    assert effective_for(1, "yellow") == 1
    assert effective_for(0, "yellow") == 0


def test_effective_for_red_caps_at_1():
    assert effective_for(4, "red") == 1
    assert effective_for(3, "red") == 1
    assert effective_for(2, "red") == 1
    assert effective_for(1, "red") == 1
    assert effective_for(0, "red") == 0


def test_effective_for_unknown_treats_as_green():
    """Unknown / None state → no cap (ceiling 4)."""
    assert effective_for(4, "unknown") == 4
    assert effective_for(4, None) == 4


def test_effective_for_string_scope_levels():
    """String scope levels (L3, '3', 'supervised') are coerced to ints."""
    assert effective_for("L3", "green") == 3
    assert effective_for("3", "green") == 3
    assert effective_for("supervised", "green") == 3
    assert effective_for("L4", "yellow") == 2  # yellow caps it
    # Legacy 4-string names: act_alone → L3, capped by yellow → 2.
    assert effective_for("act_alone", "yellow") == 2


def test_effective_for_unrecognised_scope_defaults_to_l0():
    """Garbage scope level → 0 (manual). Fail safe."""
    assert effective_for("garbage", "green") == 0
    assert effective_for(None, "green") == 0


# ---------------------------------------------------------------------------
# scope_level (DB read)
# ---------------------------------------------------------------------------

def test_scope_level_missing_row_returns_zero(solomon_db):
    """No row in scope_autonomy → L0 default."""
    assert scope_level("default", "pricing") == 0


def test_scope_level_reads_l_prefix(solomon_db):
    """'L3' stored in the table → 3 returned."""
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO scope_autonomy (tenant_id, scope, level) VALUES (?, ?, ?)",
                ("default", "pricing", "L3"),
            )
        conn.commit()
    assert scope_level("default", "pricing") == 3


def test_scope_level_reads_int_string(solomon_db):
    """'2' stored as a bare int-string → 2."""
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO scope_autonomy (tenant_id, scope, level) VALUES (?, ?, ?)",
                ("default", "ops", "2"),
            )
        conn.commit()
    assert scope_level("default", "ops") == 2


def test_scope_level_empty_scope_returns_zero():
    """None/empty scope → 0."""
    assert scope_level("default", None) == 0
    assert scope_level("default", "") == 0


# ---------------------------------------------------------------------------
# Legacy AutonomyLadder class (smoke test — conductor uses this)
# ---------------------------------------------------------------------------

def test_autonomy_ladder_level_for_default(solomon_db, monkeypatch):
    """Legacy class returns 'watch' for an unmapped scope."""
    monkeypatch.delenv("SOLOMON_TENANT_ID", raising=False)
    from solomon.storage import decisions
    decisions.reset_tenant_cache()

    al = ladder.AutonomyLadder(adapter=None)
    assert al.level_for("anything") == "watch"
    assert al.level_for(None) == "watch"


def test_autonomy_ladder_level_for_translates_l_prefix(solomon_db, monkeypatch):
    """Storing 'L3' in scope_autonomy → AutonomyLadder.level_for returns 'act_alone'."""
    monkeypatch.delenv("SOLOMON_TENANT_ID", raising=False)
    from solomon.storage import decisions
    decisions.reset_tenant_cache()
    decisions.get_or_create_tenant_id()

    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO scope_autonomy (tenant_id, scope, level) VALUES (?, ?, ?)",
                ("default", "scheduling", "L3"),
            )
        conn.commit()

    al = ladder.AutonomyLadder(adapter=None)
    assert al.level_for("scheduling") == "act_alone"

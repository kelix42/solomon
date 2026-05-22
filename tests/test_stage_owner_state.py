"""Test for solomon.pipeline.stage_owner_state."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from solomon.pipeline import stage_owner_state
from solomon.storage.pool import cursor, execute, get_conn

from tests._pipeline_helpers import read_event, seed_event


def _insert_biometric(state: str, hours_ago: float = 1.0, payload: dict | None = None):
    recorded = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO biometrics (tenant_id, recorded_at, state, source, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                ("default", recorded.isoformat(), state, "manual", json.dumps(payload or {})),
            )
        conn.commit()


def test_owner_state_no_row_defaults_to_unknown(solomon_db):
    """No biometrics row → state='unknown', ceiling=4."""
    seed_event(event_id="ev-os-none")
    row = stage_owner_state.run("ev-os-none", {
        "event_id": "ev-os-none",
        "tenant_id": "default",
    })
    assert row["owner_state"] == "unknown"
    assert row["owner_state_ceiling"] == 4

    persisted = read_event("ev-os-none")
    assert persisted["owner_state"] == "unknown"
    assert persisted["owner_state_ceiling"] == 4


@pytest.mark.parametrize("state,expected_ceiling", [
    ("green", 4),
    ("yellow", 2),
    ("red", 1),
])
def test_owner_state_recent_row(solomon_db, state, expected_ceiling):
    """Recent green/yellow/red rows map to the correct ceiling."""
    _insert_biometric(state, hours_ago=1.0)
    seed_event(event_id=f"ev-os-{state}")
    row = stage_owner_state.run(f"ev-os-{state}", {
        "event_id": f"ev-os-{state}",
        "tenant_id": "default",
    })
    assert row["owner_state"] == state
    assert row["owner_state_ceiling"] == expected_ceiling


def test_owner_state_stale_row_is_unknown(solomon_db):
    """A biometrics row > 24h old is treated as unknown."""
    _insert_biometric("green", hours_ago=48.0)
    seed_event(event_id="ev-os-stale")
    row = stage_owner_state.run("ev-os-stale", {
        "event_id": "ev-os-stale",
        "tenant_id": "default",
    })
    assert row["owner_state"] == "unknown"
    assert row["owner_state_ceiling"] == 4


def test_owner_state_derives_from_payload(solomon_db):
    """A row with state='' but rich payload still derives a state."""
    recorded = datetime.now(timezone.utc) - timedelta(hours=1)
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO biometrics (tenant_id, recorded_at, state, source, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "default", recorded.isoformat(), "raw",  # non-categorical
                    "whoop",
                    json.dumps({"recovery_pct": 25.0, "sleep_hours": 6.0, "stress_flag": False}),
                ),
            )
        conn.commit()
    seed_event(event_id="ev-os-payload")
    row = stage_owner_state.run("ev-os-payload", {
        "event_id": "ev-os-payload",
        "tenant_id": "default",
    })
    # Recovery 25 < 33 → red.
    assert row["owner_state"] == "red"
    assert row["owner_state_ceiling"] == 1

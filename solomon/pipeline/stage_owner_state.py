"""Stage 9 — Owner-state gate.

Drive source: ``orchestrator/pipeline/stage_owner_state.py``. Report §3
line 49 — "Deterministic, reads ``db.biometrics``. green/yellow/red/
unknown from recovery_pct, sleep_hours, stress_flag".

Reads the most recent biometrics row within the last 24h for the
tenant, derives owner_state, maps to ceiling via
``solomon.autonomy.ladder.owner_state_ceiling``. If no fresh row →
state ``'unknown'`` and ceiling ``4`` (no cap), per spec.

The ``biometrics`` schema in this repo stores the categorical state
directly in ``state`` (green/yellow/red/unknown) plus a JSON ``payload``
with the raw signals. Drive's schema split them into separate columns;
both paths are supported here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from ..autonomy.ladder import owner_state_ceiling
from ..storage.pool import cursor, execute, get_conn, parse_json
from ._helpers import update_event

logger = logging.getLogger("solomon.pipeline.owner_state")


_STALE_THRESHOLD = timedelta(hours=24)


def _parse_timestamp(raw) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
    s = str(raw).strip()
    if not s:
        return None
    # ISO 8601 with optional Z.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    # SQLite's datetime('now') format: "YYYY-MM-DD HH:MM:SS"
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _derive_state_from_payload(payload: dict) -> str:
    """Map raw Whoop-style signals (recovery_pct/sleep_hours/stress_flag)
    to a green/yellow/red bucket. Matches Drive's stage_owner_state."""
    recovery = payload.get("recovery_pct")
    sleep = payload.get("sleep_hours")
    stress = bool(payload.get("stress_flag"))
    try:
        recovery_val = float(recovery) if recovery is not None else None
    except (TypeError, ValueError):
        recovery_val = None
    try:
        sleep_val = float(sleep) if sleep is not None else None
    except (TypeError, ValueError):
        sleep_val = None

    if (recovery_val is not None and recovery_val < 33) or stress:
        return "red"
    if (recovery_val is not None and recovery_val < 60) or (sleep_val is not None and sleep_val < 7):
        return "yellow"
    if recovery_val is not None or sleep_val is not None:
        return "green"
    return "unknown"


def _fetch_latest_biometric(tenant_id: str) -> Tuple[Optional[str], Optional[datetime], Optional[dict]]:
    """Return (state, recorded_at, payload) for the most recent row."""
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "SELECT state, recorded_at, payload FROM biometrics "
                    "WHERE tenant_id = ? ORDER BY recorded_at DESC LIMIT 1",
                    (tenant_id,),
                )
                row = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning("stage_owner_state: DB read failed: %s", e)
        return None, None, None
    if row is None:
        return None, None, None
    state = row[0] if not hasattr(row, "keys") else row["state"]
    recorded_at_raw = row[1] if not hasattr(row, "keys") else row["recorded_at"]
    payload_raw = row[2] if not hasattr(row, "keys") else row["payload"]
    payload = parse_json(payload_raw) if payload_raw is not None else {}
    if not isinstance(payload, dict):
        payload = {}
    return (str(state) if state else None), _parse_timestamp(recorded_at_raw), payload


def run(event_id: str, event_row: dict) -> dict:
    """Read biometrics, write owner_state and owner_state_ceiling."""
    tenant_id = event_row.get("tenant_id") or "default"

    state, recorded_at, payload = _fetch_latest_biometric(tenant_id)

    if state is None or recorded_at is None:
        owner_state = "unknown"
    else:
        # Stale → unknown (no cap), matching the Drive "warn once, default green" rule.
        now = datetime.now(timezone.utc)
        if (now - recorded_at) > _STALE_THRESHOLD:
            owner_state = "unknown"
        else:
            # If the stored ``state`` is already categorical use it; otherwise
            # derive from the payload (Drive-style schemas).
            s = state.strip().lower()
            if s in {"green", "yellow", "red", "unknown"}:
                owner_state = s
            else:
                owner_state = _derive_state_from_payload(payload or {})

    ceiling = owner_state_ceiling(owner_state)

    update_event(event_id, owner_state=owner_state, owner_state_ceiling=ceiling)
    event_row["owner_state"] = owner_state
    event_row["owner_state_ceiling"] = ceiling
    return event_row

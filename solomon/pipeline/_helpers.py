"""Pipeline helpers.

Drive source: ``orchestrator/pipeline/_helpers.py``. Report ref §1.1 lines
52-55 — "``stage_timer`` context manager that writes per-stage elapsed_ms
into the JSON column ``events.stage_timings_ms``".

All DB access goes through ``solomon.storage.pool``. No raw sqlite3 /
psycopg here.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from ..storage.pool import cursor, execute, get_conn, parse_json, row_to_dict

logger = logging.getLogger("solomon.pipeline._helpers")

# Columns that hold JSON strings; ``get_event`` decodes these on read.
_JSON_COLUMNS = (
    "participants",
    "channel_metadata",
    "classification",
    "retrieval_context",
    "stage_timings_ms",
)

# Columns that the pipeline is allowed to update via ``update_event``.
# Whitelist guards against typos sneaking through.
_UPDATABLE_COLUMNS = frozenset({
    "salience_score",
    "classification",
    "hard_rule_verdict",
    "hard_rule_reason",
    "retrieval_context",
    "system1_output",
    "system2_output",
    "divergence_score",
    "audit_verdict",
    "audit_reasoning",
    "owner_state",
    "action_taken",
    "status",
    "stage_timings_ms",
    "completed_at",
})


def get_event(event_id: str) -> Optional[Dict[str, Any]]:
    """Read one events row, decode JSON columns, return as a dict.

    Returns ``None`` if no row exists for ``event_id``.
    """
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(cur, "SELECT * FROM events WHERE event_id = ? LIMIT 1", (event_id,))
                row = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning("get_event(%s) failed: %s", event_id, e)
        return None
    if row is None:
        return None
    d = row_to_dict(row)
    for col in _JSON_COLUMNS:
        if col in d and d[col] is not None:
            d[col] = parse_json(d[col]) or d[col]
    return d


def update_event(event_id: str, **fields: Any) -> None:
    """Partial UPDATE on the events row.

    JSON-shaped values are auto-serialised. ``stage_timings_ms`` is treated
    as a merge: pass ``stage_timings_ms={"salience": 42}`` and only that
    key gets touched; existing keys are preserved.

    Unknown column names are dropped with a warning rather than blowing up
    the pipeline.
    """
    if not fields:
        return
    safe: Dict[str, Any] = {}
    timings_patch: Optional[Dict[str, Any]] = None
    for k, v in fields.items():
        if k not in _UPDATABLE_COLUMNS:
            logger.warning("update_event: ignoring unknown column %r", k)
            continue
        if k == "stage_timings_ms" and isinstance(v, dict):
            timings_patch = v
            continue
        if isinstance(v, (dict, list)):
            safe[k] = json.dumps(v, default=str)
        else:
            safe[k] = v

    try:
        with get_conn() as conn:
            # Merge stage_timings_ms if requested.
            if timings_patch is not None:
                with cursor(conn) as cur:
                    execute(cur, "SELECT stage_timings_ms FROM events WHERE event_id = ?", (event_id,))
                    row = cur.fetchone()
                existing: Dict[str, Any] = {}
                if row is not None:
                    raw = row[0] if not hasattr(row, "keys") else row["stage_timings_ms"]
                    parsed = parse_json(raw)
                    if isinstance(parsed, dict):
                        existing = parsed
                existing.update(timings_patch)
                safe["stage_timings_ms"] = json.dumps(existing, default=str)

            if not safe:
                conn.commit()
                return

            set_clause = ", ".join(f"{col} = ?" for col in safe.keys())
            params = list(safe.values()) + [event_id]
            with cursor(conn) as cur:
                execute(cur, f"UPDATE events SET {set_clause} WHERE event_id = ?", params)
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("update_event(%s, fields=%s) failed: %s", event_id, list(fields), e)


def set_event_status(event_id: str, status: str, reason: Optional[str] = None) -> None:
    """Transition the events row to a new status.

    Valid statuses: pending | in_progress | complete | skipped | failed |
    blocked_by_hard_rule. ``reason`` is appended to stage_timings_ms under
    the synthetic key ``_status_reason`` for debugging.
    """
    fields: Dict[str, Any] = {"status": status}
    if status in {"complete", "skipped", "blocked_by_hard_rule", "failed"}:
        # Record the completion timestamp via a separate cheap UPDATE so the
        # caller doesn't have to know about it.
        try:
            with get_conn() as conn:
                with cursor(conn) as cur:
                    execute(
                        cur,
                        "UPDATE events SET completed_at = COALESCE(completed_at, datetime('now')) "
                        "WHERE event_id = ?",
                        (event_id,),
                    )
                conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("set_event_status completed_at touch failed: %s", e)
    if reason is not None:
        fields["stage_timings_ms"] = {"_status_reason": reason}
    update_event(event_id, **fields)


@contextmanager
def stage_timer(event_id: str, stage_name: str) -> Iterator[Dict[str, Any]]:
    """Context manager that times a stage and records elapsed ms on exit.

    On exception inside the block, writes a ``<stage>_error`` key into
    ``stage_timings_ms`` instead of suppressing — the caller decides whether
    to halt the pipeline.

    Yields a small mutable dict the stage can write debug fields into; we
    don't currently persist those (they'd noise up the audit trail) but
    they're useful for the runner to log on failure.
    """
    box: Dict[str, Any] = {}
    started = time.perf_counter()
    try:
        yield box
    except Exception as e:  # noqa: BLE001
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        update_event(
            event_id,
            stage_timings_ms={
                stage_name: elapsed_ms,
                f"{stage_name}_error": f"{type(e).__name__}: {e}",
            },
        )
        raise
    else:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        update_event(event_id, stage_timings_ms={stage_name: elapsed_ms})

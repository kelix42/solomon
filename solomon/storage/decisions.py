"""Decision logging — every turn that isn't private gets one row here.

Part 12 of the design doc. Every column we store becomes a signal for
pattern detection, drift checks, calibration scoring, mentoring
questions, and debugging.

Two write paths exist:

  * ``DecisionLog.log(turn)`` — the conductor's per-turn TurnContext
    mirror (unchanged public shape; called from ``conductor._post_llm_call``).

  * ``mirror_event_to_decision(event_id)`` — the pipeline's "after the
    10 stages finish, copy the events row into a decisions row for the
    H2 audit log". The runner calls this in stage_action when an event
    completes.

Both go through ``solomon.storage.pool`` — ``get_conn`` / ``cursor`` /
``execute`` / ``jsonify`` — with ``?`` placeholders. No raw ``%s``,
no direct psycopg or sqlite3 imports. The previous version used ``%s``
directly and crashed on SQLite (which is the default backend).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from .pool import cursor, execute, get_conn, insert_returning, jsonify, parse_json, row_to_dict

logger = logging.getLogger("solomon.storage.decisions")

_TENANT_ID_CACHE: Optional[str] = None
_DEFAULT_TENANT = "default"


def get_or_create_tenant_id() -> str:
    """Return the active tenant_id. For single-tenant installs (the common
    case for one business owner using Solomon), this is the value of
    ``SOLOMON_TENANT_ID`` env var, or ``'default'``.

    Creates the tenant row on first call if it doesn't exist. Uses the
    portable pool API so it works on both SQLite and Postgres.
    """
    global _TENANT_ID_CACHE
    if _TENANT_ID_CACHE is not None:
        return _TENANT_ID_CACHE

    tenant_id = os.getenv("SOLOMON_TENANT_ID", _DEFAULT_TENANT)
    business_name = os.getenv("SOLOMON_BUSINESS_NAME", "My Business")

    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                # OR IGNORE works on SQLite; on Postgres the portable form
                # is ON CONFLICT DO NOTHING. Use the portable two-step.
                execute(
                    cur,
                    "SELECT 1 FROM tenants WHERE tenant_id = ? LIMIT 1",
                    (tenant_id,),
                )
                exists = cur.fetchone() is not None
                if not exists:
                    execute(
                        cur,
                        "INSERT INTO tenants (tenant_id, business_name) VALUES (?, ?)",
                        (tenant_id, business_name),
                    )
            conn.commit()
        _TENANT_ID_CACHE = tenant_id
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not ensure tenant row for %s: %s", tenant_id, e)
        _TENANT_ID_CACHE = tenant_id
    return tenant_id


def reset_tenant_cache() -> None:
    """Clear the module-level tenant cache. Tests call this between
    fixtures so the cached value from a prior temp DB doesn't leak."""
    global _TENANT_ID_CACHE
    _TENANT_ID_CACHE = None


# ---------------------------------------------------------------------------
# Events → decisions mirror (the pipeline's audit-trail handoff)
# ---------------------------------------------------------------------------

def mirror_event_to_decision(event_id: str) -> Optional[int]:
    """Copy the relevant columns from the events row into a decisions row.

    Called by ``stage_action`` once the 10-stage pipeline reaches
    ``status='complete'``. Returns the inserted ``decision_id``, or
    ``None`` if the events row is missing.

    The decisions table is the long-lived H2 audit-log shape; the events
    row is the transient per-stage record. They overlap, but the
    decisions row carries the canonical column set the sleep cycle
    reasons over.
    """
    with get_conn() as conn:
        with cursor(conn) as cur:
            # Idempotency: if a decisions row already exists for this
            # event_id (stage_action already mirrored, or a previous
            # post_llm_call ran), return the existing id rather than
            # inserting a duplicate. The conductor's post_llm_call
            # hook calls this too, so without this guard inline-mode
            # turns would produce two decisions rows per event.
            execute(
                cur,
                "SELECT decision_id FROM decisions WHERE event_id = ? "
                "ORDER BY decision_id ASC LIMIT 1",
                (event_id,),
            )
            existing = cur.fetchone()
            if existing is not None:
                logger.debug(
                    "mirror_event_to_decision: decisions row already exists "
                    "for event_id=%s (decision_id=%s); skipping insert",
                    event_id, existing[0],
                )
                return int(existing[0])

            execute(cur, "SELECT * FROM events WHERE event_id = ? LIMIT 1", (event_id,))
            row = cur.fetchone()
            if row is None:
                logger.warning("mirror_event_to_decision: event_id=%s not found", event_id)
                return None
            ev = row_to_dict(row)

        # Decode the JSON columns we care about.
        classification = parse_json(ev.get("classification")) or {}
        if not isinstance(classification, dict):
            classification = {}
        retrieval = parse_json(ev.get("retrieval_context")) or {}
        if not isinstance(retrieval, dict):
            retrieval = {}

        system2_blob = parse_json(ev.get("system2_output")) or {}
        if isinstance(system2_blob, dict):
            proposed_action = (
                system2_blob.get("proposed_action")
                or system2_blob.get("answer")
                or ""
            )
        else:
            proposed_action = ""

        scope = classification.get("scope")
        domain = classification.get("domain")
        decision_type = classification.get("decision_type")
        confidence_raw = classification.get("confidence")
        try:
            classification_confidence = float(confidence_raw) if confidence_raw is not None else None
        except (TypeError, ValueError):
            classification_confidence = None

        effective_int = ev.get("effective_autonomy")
        try:
            autonomy_level_at_time = (
                f"L{int(effective_int)}" if effective_int is not None else None
            )
        except (TypeError, ValueError):
            autonomy_level_at_time = None

        params = (
            ev.get("tenant_id"),
            event_id,
            scope,
            domain,
            decision_type,
            classification_confidence,
            ev.get("salience_score"),
            1 if retrieval.get("working_memory_used") else 0,
            jsonify(retrieval.get("lanes_used") or []),
            jsonify(retrieval.get("heuristic_ids") or []),
            jsonify(retrieval.get("foundation_files") or []),
            ev.get("system1_output"),
            ev.get("system2_output"),
            ev.get("divergence_score"),
            proposed_action or None,
            ev.get("audit_verdict"),
            ev.get("audit_reasoning"),
            ev.get("action_taken"),
            autonomy_level_at_time,
        )

        decision_id = insert_returning(
            conn,
            """
            INSERT INTO decisions (
                tenant_id, event_id, scope, domain, decision_type,
                classification_confidence, salience_score,
                working_memory_used, retrieval_lanes_used,
                heuristics_referenced, foundation_files_used,
                system_1_answer, system_2_answer, divergence_score,
                proposed_action, audit_verdict, audit_reasoning,
                final_action, autonomy_level_at_time
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?
            ) RETURNING decision_id
            """,
            params,
        )

        # Mirror to audit_log if there's a verdict.
        verdict = ev.get("audit_verdict")
        if verdict and decision_id is not None:
            try:
                with cursor(conn) as cur:
                    execute(
                        cur,
                        "INSERT INTO audit_log (tenant_id, decision_id, verdict, reasoning) "
                        "VALUES (?, ?, ?, ?)",
                        (ev.get("tenant_id"), decision_id, verdict, ev.get("audit_reasoning")),
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("audit_log insert failed for decision_id=%s: %s", decision_id, e)

        conn.commit()
    return decision_id


# ---------------------------------------------------------------------------
# Conductor's per-turn write (unchanged public shape)
# ---------------------------------------------------------------------------

class DecisionLog:
    """Writes one row per non-private turn into the decisions table.

    Called from ``conductor._post_llm_call``. Uses the portable pool
    API; works on SQLite and Postgres.
    """

    def __init__(self, adapter) -> None:  # noqa: ANN001
        self.adapter = adapter

    def log(self, turn) -> int:  # noqa: ANN001  (TurnContext from conductor)
        """Persist the turn. Returns the decision_id (or 0 on failure)."""
        tenant_id = get_or_create_tenant_id()

        # Ensure the events row exists first so the FK is valid.
        if turn.raw_event is not None:
            try:
                self._upsert_event(tenant_id, turn.raw_event, turn.salience_score)
            except Exception as e:  # noqa: BLE001
                logger.warning("DecisionLog: event upsert failed: %s", e)

        params = (
            tenant_id,
            turn.raw_event.id if turn.raw_event else None,
            turn.scope,
            turn.domain,
            turn.decision_type,
            turn.classification_confidence,
            turn.salience_score,
            1 if turn.working_memory_used else 0,
            jsonify(turn.retrieval_lanes_used),
            jsonify(turn.heuristics_referenced),
            jsonify(turn.foundation_files_used),
            turn.system_1_answer,
            turn.system_2_answer,
            turn.divergence_score,
            turn.proposed_action,
            turn.audit_verdict,
            turn.audit_reasoning,
            turn.final_action,
            turn.autonomy_level_at_time,
        )

        try:
            with get_conn() as conn:
                decision_id = insert_returning(
                    conn,
                    """
                    INSERT INTO decisions (
                        tenant_id, event_id, scope, domain, decision_type,
                        classification_confidence, salience_score,
                        working_memory_used, retrieval_lanes_used,
                        heuristics_referenced, foundation_files_used,
                        system_1_answer, system_2_answer, divergence_score,
                        proposed_action, audit_verdict, audit_reasoning,
                        final_action, autonomy_level_at_time
                    ) VALUES (
                        ?, ?, ?, ?, ?,
                        ?, ?,
                        ?, ?,
                        ?, ?,
                        ?, ?, ?,
                        ?, ?, ?,
                        ?, ?
                    ) RETURNING decision_id
                    """,
                    params,
                )
                decision_id = int(decision_id) if decision_id is not None else 0

                # Audit log row too, if there's a verdict.
                if turn.audit_verdict and decision_id:
                    with cursor(conn) as cur:
                        execute(
                            cur,
                            "INSERT INTO audit_log (tenant_id, decision_id, verdict, reasoning) "
                            "VALUES (?, ?, ?, ?)",
                            (tenant_id, decision_id, turn.audit_verdict, turn.audit_reasoning),
                        )
                conn.commit()
                return decision_id
        except Exception as e:  # noqa: BLE001
            logger.warning("DecisionLog.log failed: %s", e)
            return 0

    @staticmethod
    def _upsert_event(tenant_id: str, raw_event, salience_score: Optional[float]) -> None:  # noqa: ANN001
        """Ensure an events row exists for ``raw_event``. Portable
        delete-then-insert because INSERT...ON CONFLICT syntax differs
        between SQLite and Postgres (see references/storage-patterns.md).
        """
        row = raw_event.to_db_row(tenant_id=tenant_id, salience_score=salience_score)
        received_at = row["received_at"]
        if hasattr(received_at, "isoformat"):
            received_at = received_at.isoformat()
        processed_at = row.get("processed_at")
        if hasattr(processed_at, "isoformat"):
            processed_at = processed_at.isoformat()

        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "SELECT event_id FROM events WHERE event_id = ? LIMIT 1",
                    (row["event_id"],),
                )
                exists = cur.fetchone() is not None
                if exists:
                    execute(
                        cur,
                        "UPDATE events SET salience_score = ?, completed_at = ? "
                        "WHERE event_id = ?",
                        (row.get("salience_score"), processed_at, row["event_id"]),
                    )
                else:
                    execute(
                        cur,
                        "INSERT INTO events ("
                        "  event_id, tenant_id, source, received_at, participants, "
                        "  raw_content, channel_metadata, salience_score, completed_at, private"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            row["event_id"],
                            row["tenant_id"],
                            row["source"],
                            received_at,
                            row["participants"],
                            row["raw_content"],
                            row["channel_metadata"],
                            row.get("salience_score"),
                            processed_at,
                            1 if row.get("private") else 0,
                        ),
                    )
            conn.commit()

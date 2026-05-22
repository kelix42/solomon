"""Decision logging — every turn that isn't private gets one row here.

Part 12 of the design doc. Every column we store becomes a signal for
pattern detection, drift checks, calibration scoring, mentoring
questions, and debugging.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from .pool import get_pool

logger = logging.getLogger("solomon.storage.decisions")

_TENANT_ID_CACHE: Optional[str] = None
_DEFAULT_TENANT = "default"


def get_or_create_tenant_id() -> str:
    """Return the active tenant_id. For single-tenant installs (the common
    case for one business owner using Solomon), this is the value of
    SOLOMON_TENANT_ID env var, or 'default'.

    Creates the tenant row on first call if it doesn't exist.
    """
    global _TENANT_ID_CACHE
    if _TENANT_ID_CACHE is not None:
        return _TENANT_ID_CACHE

    tenant_id = os.getenv("SOLOMON_TENANT_ID", _DEFAULT_TENANT)
    business_name = os.getenv("SOLOMON_BUSINESS_NAME", "My Business")

    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO tenants (tenant_id, business_name) "
                    "VALUES (%s, %s) ON CONFLICT (tenant_id) DO NOTHING;",
                    (tenant_id, business_name),
                )
            conn.commit()
        _TENANT_ID_CACHE = tenant_id
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not ensure tenant row for %s: %s", tenant_id, e)
        _TENANT_ID_CACHE = tenant_id
    return tenant_id


class DecisionLog:
    """Writes one row per non-private turn into the decisions table."""

    def __init__(self, adapter) -> None:  # noqa: ANN001
        self.adapter = adapter

    def log(self, turn) -> int:  # noqa: ANN001  (TurnContext from conductor)
        """Persist the turn. Returns the decision_id."""
        tenant_id = get_or_create_tenant_id()
        # Ensure the raw_event row exists first, so the FK is valid.
        if turn.raw_event is not None:
            self._upsert_raw_event(tenant_id, turn.raw_event, turn.salience_score)

        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
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
                        %s, %s, %s, %s, %s,
                        %s, %s,
                        %s, %s,
                        %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s
                    ) RETURNING decision_id;
                    """,
                    (
                        tenant_id,
                        turn.raw_event.id if turn.raw_event else None,
                        turn.scope,
                        turn.domain,
                        turn.decision_type,
                        turn.classification_confidence,
                        turn.salience_score,
                        turn.working_memory_used,
                        json.dumps(turn.retrieval_lanes_used),
                        json.dumps(turn.heuristics_referenced),
                        json.dumps(turn.foundation_files_used),
                        turn.system_1_answer,
                        turn.system_2_answer,
                        turn.divergence_score,
                        turn.proposed_action,
                        turn.audit_verdict,
                        turn.audit_reasoning,
                        turn.final_action,
                        turn.autonomy_level_at_time,
                    ),
                )
                row = cur.fetchone()
                decision_id = int(row[0]) if row else 0

                # Audit log row too.
                if turn.audit_verdict:
                    cur.execute(
                        "INSERT INTO audit_log (tenant_id, decision_id, verdict, reasoning) "
                        "VALUES (%s, %s, %s, %s);",
                        (tenant_id, decision_id, turn.audit_verdict, turn.audit_reasoning),
                    )
            conn.commit()
            return decision_id

    @staticmethod
    def _upsert_raw_event(tenant_id: str, raw_event, salience_score: Optional[float]) -> None:  # noqa: ANN001
        row = raw_event.to_db_row(tenant_id=tenant_id, salience_score=salience_score)
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO raw_events (
                        event_id, tenant_id, source, received_at, participants,
                        raw_content, channel_metadata, salience_score, processed_at, private
                    ) VALUES (
                        %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s, %s
                    ) ON CONFLICT (event_id) DO UPDATE SET
                        salience_score = EXCLUDED.salience_score,
                        processed_at = EXCLUDED.processed_at;
                    """,
                    (
                        row["event_id"], row["tenant_id"], row["source"], row["received_at"],
                        row["participants"], row["raw_content"], row["channel_metadata"],
                        row["salience_score"], row["processed_at"], row["private"],
                    ),
                )
            conn.commit()

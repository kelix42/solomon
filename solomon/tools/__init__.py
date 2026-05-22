"""Solomon tools that the LLM can call from inside Hermes.

These appear in the Hermes tool list under the ``solomon`` toolset. The
LLM uses them to log decisions deliberately, store predictions, check the
audit gate, look up its autonomy level, etc.

All of them are also called by the conductor internally during the
hot path, but exposing them gives the model the option to invoke them
on purpose (e.g. "this is high-salience, log it explicitly with the
following metadata").
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

logger = logging.getLogger("solomon.tools")


def register_all(adapter, conductor) -> None:  # noqa: ANN001
    """Register every Solomon tool with Hermes.

    Called once at plugin startup from conductor.register_tools().
    """
    _register_audit_gate(adapter, conductor)
    _register_autonomy(adapter, conductor)
    _register_log_decision(adapter, conductor)
    _register_store_prediction(adapter, conductor)
    _register_salience(adapter, conductor)


def _register_audit_gate(adapter, conductor) -> None:  # noqa: ANN001
    def handler(args: Dict[str, Any], **kw: Any) -> str:
        proposed = args.get("proposed_action", "")
        scope = args.get("scope")
        try:
            verdict = conductor.audit_gate.run(
                proposed_action=proposed,
                context=type("Ctx", (), {"items": []})(),
                surprise=float(args.get("surprise", 0.0)),
                scope=scope,
            )
            return json.dumps({
                "verdict": verdict.verdict,
                "reasoning": verdict.reasoning,
                "checks_passed": verdict.checks_passed,
                "checks_failed": verdict.checks_failed,
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("solomon_audit tool failed: %s", e)
            return json.dumps({"verdict": "downgrade", "reasoning": str(e)})

    adapter.register_tool(
        name="solomon_audit",
        description="Run the Solomon audit gate against a proposed action. Returns JSON with verdict (approve|downgrade|reject|request_rethink), reasoning, checks_passed, checks_failed.",
        parameters={
            "type": "object",
            "properties": {
                "proposed_action": {"type": "string", "description": "The action being audited."},
                "scope": {"type": "string", "description": "Business scope (pricing, hiring, etc.)."},
                "surprise": {"type": "number", "description": "Divergence score 0-1 from System 1 / System 2."},
            },
            "required": ["proposed_action"],
        },
        handler=handler,
    )


def _register_autonomy(adapter, conductor) -> None:  # noqa: ANN001
    def handler(args: Dict[str, Any], **kw: Any) -> str:
        scope = args.get("scope")
        try:
            level = conductor.autonomy.level_for(scope)
            observe_only = conductor.autonomy.is_observe_only()
            return json.dumps({"scope": scope, "level": level, "observe_only_window": observe_only})
        except Exception as e:  # noqa: BLE001
            return json.dumps({"scope": scope, "level": "watch", "error": str(e)})

    adapter.register_tool(
        name="solomon_autonomy_level",
        description="Look up the current autonomy level for a scope (watch|suggest|act_with_approval|act_alone) and whether the tenant is still in the 30-day observe-only window.",
        parameters={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "Business scope to query."},
            },
            "required": ["scope"],
        },
        handler=handler,
    )


def _register_log_decision(adapter, conductor) -> None:  # noqa: ANN001
    def handler(args: Dict[str, Any], **kw: Any) -> str:
        """Manual decision logging. Most decisions are auto-logged via the
        post_llm_call hook, but the LLM can call this for cases it wants
        to flag explicitly.
        """
        try:
            from .conductor import TurnContext
            from .capture.raw_event import RawEvent
            from datetime import datetime, timezone
            import uuid

            scope = args.get("scope")
            text = args.get("description", "")
            raw = RawEvent(
                id=f"manual:{uuid.uuid4().hex[:8]}",
                source="manual_log",
                received_at=datetime.now(timezone.utc),
                participants=[],
                raw_content=text,
                channel_metadata={},
            )
            turn = TurnContext(
                raw_event=raw,
                scope=scope,
                salience_score=float(args.get("salience", 0.5)),
                system_2_answer=text,
                proposed_action=args.get("proposed_action", ""),
                final_action=args.get("final_action", ""),
                audit_verdict=args.get("verdict", "approve"),
            )
            decision_id = conductor.decision_log.log(turn)
            return json.dumps({"decision_id": decision_id, "status": "logged"})
        except Exception as e:  # noqa: BLE001
            logger.warning("solomon_log_decision tool failed: %s", e)
            return json.dumps({"status": "error", "reason": str(e)})

    adapter.register_tool(
        name="solomon_log_decision",
        description="Log a decision explicitly into Solomon's decision store. Use sparingly — most decisions are auto-logged by the conductor.",
        parameters={
            "type": "object",
            "properties": {
                "scope": {"type": "string"},
                "description": {"type": "string"},
                "proposed_action": {"type": "string"},
                "final_action": {"type": "string"},
                "salience": {"type": "number"},
                "verdict": {"type": "string"},
            },
            "required": ["scope", "description"],
        },
        handler=handler,
    )


def _register_store_prediction(adapter, conductor) -> None:  # noqa: ANN001
    def handler(args: Dict[str, Any], **kw: Any) -> str:
        try:
            decision_id = int(args.get("decision_id", 0))
            prediction = args.get("prediction", "")
            expected_by_days = int(args.get("expected_by_days", 7))
            from datetime import datetime, timezone, timedelta
            from .storage.pool import get_pool
            from .storage.decisions import get_or_create_tenant_id
            tenant_id = get_or_create_tenant_id()
            expected_by = datetime.now(timezone.utc) + timedelta(days=expected_by_days)
            with get_pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO predictions (tenant_id, decision_id, prediction_text, expected_by) "
                        "VALUES (%s, %s, %s, %s) RETURNING prediction_id;",
                        (tenant_id, decision_id, prediction, expected_by),
                    )
                    row = cur.fetchone()
                conn.commit()
            return json.dumps({"prediction_id": int(row[0]) if row else None, "expected_by": expected_by.isoformat()})
        except Exception as e:  # noqa: BLE001
            return json.dumps({"status": "error", "reason": str(e)})

    adapter.register_tool(
        name="solomon_store_prediction",
        description="Store a checkpoint prediction tied to a decision. Use when the LLM wants to commit to a verifiable expected outcome by a specific date.",
        parameters={
            "type": "object",
            "properties": {
                "decision_id": {"type": "integer"},
                "prediction": {"type": "string"},
                "expected_by_days": {"type": "integer", "description": "Days from now until the prediction is checked."},
            },
            "required": ["decision_id", "prediction", "expected_by_days"],
        },
        handler=handler,
    )


def _register_salience(adapter, conductor) -> None:  # noqa: ANN001
    def handler(args: Dict[str, Any], **kw: Any) -> str:
        try:
            from .capture.raw_event import RawEvent
            from datetime import datetime, timezone
            import uuid
            text = args.get("content", "")
            raw = RawEvent(
                id=f"manual:{uuid.uuid4().hex[:8]}",
                source="manual_score",
                received_at=datetime.now(timezone.utc),
                participants=[],
                raw_content=text,
                channel_metadata={},
            )
            result = conductor.salience.score(raw, scope=args.get("scope"))
            return json.dumps({"score": result.score, "breakdown": result.breakdown})
        except Exception as e:  # noqa: BLE001
            return json.dumps({"score": 0.0, "error": str(e)})

    adapter.register_tool(
        name="solomon_salience",
        description="Score a piece of content for salience (stakes/novelty/emotion/owner_involvement). Returns 0-1 score and per-dimension breakdown.",
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "scope": {"type": "string"},
            },
            "required": ["content"],
        },
        handler=handler,
    )

"""The Conductor — Solomon's central routing function.

Every Hermes turn flows through here. Every gateway message becomes a
RawEvent and goes through process_event(). Every proposed action goes
through the audit gate. Every decision gets logged with its predictions
and counterfactuals.

This module is the orchestrator described in Part 4 of the design doc.
It is *not* the Brain (the Brain is the whole system); it is the central
routing logic that calls every other component in order.

The hot path is intentionally readable. Refinement, learning, and
heuristic lifecycle live in the sleep cycle (run nightly), not here.

Pipeline integration (Session B, 2026-05-25)
---------------------------------------------

``_pre_llm_call`` now drives the 10-stage decision pipeline at
``solomon.pipeline.runner.run`` instead of the legacy 7-step inline
body. Three safeguards make the change reversible:

1.  **Kill switch.**  Setting ``SOLOMON_PIPELINE_DISABLE=1`` (any
    truthy value) makes ``_pre_llm_call`` fall through to the legacy
    inline code path. The user can flip this in ``~/.hermes/.env`` and
    restart Hermes if the pipeline misbehaves in the wild — recovery
    in under a minute, no rollback needed.

2.  **try/except wrap.**  Any exception raised by the pipeline (insert,
    runner, row read-back) is caught, logged, the events row gets
    ``status='errored'``, and the legacy path runs. A pipeline crash
    must never crash a Hermes turn.

3.  **Mode env var.**  ``SOLOMON_PIPELINE_MODE`` (default ``"inline"``)
    can be set to ``"queue"`` to skip in-process pipeline execution.
    In queue mode the conductor just inserts the events row with
    ``status='pending'`` and returns; the (future) pipeline-tick
    worker picks it up.

Audit-verdict → system-message mapping (inline mode only):

* ``status='skipped'`` (low salience) — no message injected; continue.
* ``status='blocked_by_hard_rule'`` — inject a decline message
  citing the non-negotiable.
* ``status='complete'`` + ``audit_verdict='REJECT'`` — inject a
  decline message with the audit reasoning.
* ``status='complete'`` + ``audit_verdict='REQUEST_RETHINK'`` — inject
  a system message asking the LLM to reconsider.
* ``status='complete'`` + ``audit_verdict='APPROVE'`` — no message;
  continue.

Injected system messages are appended to the ``messages`` list passed
to the hook (lists are mutable; Hermes sees the append). We do **not**
overwrite ``TurnContext.system_prompt`` because that field is
overwritten elsewhere in the Hermes pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .adapter import HermesAdapter
from .capture.raw_event import RawEvent, raw_event_from_message
from .private.mode import PrivateMode

logger = logging.getLogger("solomon.conductor")


# ---------------------------------------------------------------------------
# Env-var helpers
# ---------------------------------------------------------------------------

def _pipeline_disabled() -> bool:
    """Return True iff SOLOMON_PIPELINE_DISABLE is set to a truthy value.

    Truthy = "1", "true", "yes", "on" (case-insensitive). Anything else
    (including unset) is treated as not-disabled.
    """
    val = os.getenv("SOLOMON_PIPELINE_DISABLE", "")
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _pipeline_mode() -> str:
    """Return SOLOMON_PIPELINE_MODE, defaulting to ``inline``.

    Recognised values: ``inline`` (default), ``queue``. Unknown values
    fall back to ``inline`` with a warning so a typo doesn't silently
    drop pipeline execution.
    """
    raw = (os.getenv("SOLOMON_PIPELINE_MODE", "") or "inline").strip().lower()
    if raw not in {"inline", "queue"}:
        logger.warning(
            "Unknown SOLOMON_PIPELINE_MODE=%r; falling back to 'inline'", raw,
        )
        return "inline"
    return raw or "inline"


# ---------------------------------------------------------------------------
# TurnContext
# ---------------------------------------------------------------------------

@dataclass
class TurnContext:
    """Everything the conductor accumulates as a turn flows through it.

    Created at pre_llm_call, populated step by step, finalized at
    post_llm_call. Then handed to log_decision before being discarded.
    """
    raw_event: Optional[RawEvent] = None
    # New: the pipeline-assigned ulid for this turn's events row. Set
    # in _pre_llm_call's pipeline branch, used by _post_llm_call to
    # mirror the row into decisions.
    event_id: Optional[str] = None
    scope: Optional[str] = None
    domain: Optional[str] = None
    decision_type: Optional[str] = None
    classification_confidence: float = 0.0
    # Pipeline column mirrors (populated in inline mode).
    classification: Optional[Dict[str, Any]] = None
    system1_output: Optional[Any] = None
    system2_output: Optional[Any] = None
    owner_state: Optional[str] = None
    owner_state_ceiling: Optional[int] = None
    effective_autonomy: Optional[int] = None
    action_taken: Optional[str] = None
    stage_timings_ms: Optional[Dict[str, Any]] = None
    status: Optional[str] = None

    salience_score: float = 0.0
    salience_breakdown: Dict[str, float] = field(default_factory=dict)
    working_memory_used: bool = False
    retrieval_lanes_used: List[str] = field(default_factory=list)
    heuristics_referenced: List[str] = field(default_factory=list)
    foundation_files_used: List[str] = field(default_factory=list)
    system_1_answer: Optional[str] = None
    system_2_answer: Optional[str] = None
    divergence_score: float = 0.0
    autonomy_level_at_time: Optional[str] = None
    audit_verdict: Optional[str] = None
    audit_reasoning: Optional[str] = None
    proposed_action: Optional[str] = None
    final_action: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    private: bool = False


class Conductor:
    """The central routing function. Sits between Hermes and Solomon's
    components. Knows the order things happen in.
    """

    def __init__(self, adapter: HermesAdapter, private_mode: PrivateMode) -> None:
        self.adapter = adapter
        self.private_mode = private_mode
        # In-flight turns keyed by session_id. Most of the time there's
        # exactly one; the gateway can technically have several.
        self._turns: Dict[str, TurnContext] = {}

        # Lazy-imported components. We avoid importing them at module load
        # time so the plugin module stays fast to import.
        from .salience.scorer import SalienceScorer
        from .classify.classifier import Classifier
        from .non_negotiables.check import NonNegotiableChecker
        from .memory.working import WorkingMemory
        from .memory.retrieval import MultiLaneRetrieval
        from .reasoning.system_1 import System1
        from .reasoning.system_2 import System2
        from .reasoning.divergence import divergence_score
        from .audit_gate.audit import AuditGate
        from .autonomy.ladder import AutonomyLadder
        from .storage.decisions import DecisionLog
        from .predictions.checkpoints import PredictionStore
        from .predictions.counterfactuals import CounterfactualStore

        self.salience = SalienceScorer(adapter)
        self.classifier = Classifier(adapter)
        self.non_negotiables = NonNegotiableChecker(adapter)
        self.working_memory = WorkingMemory(adapter)
        self.retrieval = MultiLaneRetrieval(adapter)
        self.s1 = System1(adapter)
        self.s2 = System2(adapter)
        self._divergence = divergence_score
        self.audit_gate = AuditGate(adapter)
        self.autonomy = AutonomyLadder(adapter)
        self.decision_log = DecisionLog(adapter)
        self.predictions = PredictionStore(adapter)
        self.counterfactuals = CounterfactualStore(adapter)

    # -- registration -------------------------------------------------------

    def register_tools(self) -> None:
        """Register Solomon's tools into the Hermes tool registry.

        These are tools the LLM can call: log a decision, store a prediction,
        check the autonomy level, etc. The conductor uses them internally
        too, but exposing them lets the model invoke them deliberately
        (e.g. "log this as a high-salience decision in pricing scope").
        """
        from .tools import register_all
        register_all(self.adapter, self)

    def attach_hooks(self) -> None:
        """Attach to Hermes lifecycle hooks. After this returns, every
        turn flows through us.
        """
        self.adapter.attach_all({
            "on_session_start":   self._on_session_start,
            "on_session_end":     self._on_session_end,
            "pre_llm_call":       self._pre_llm_call,
            "post_llm_call":      self._post_llm_call,
            "pre_tool_call":      self._pre_tool_call,
            "post_tool_call":     self._post_tool_call,
            "pre_gateway_dispatch": self._pre_gateway_dispatch,
        })

    # -- hook handlers ------------------------------------------------------

    def _on_session_start(self, session_id: str = "", **kwargs: Any) -> None:
        logger.debug("Solomon session start: %s", session_id)
        # Private mode starts off by default; the /private command toggles it.
        self.private_mode.on_session_start(session_id)

    def _on_session_end(self, session_id: str = "", **kwargs: Any) -> None:
        logger.debug("Solomon session end: %s", session_id)
        # Finalize anything still open. Drop any in-flight turn state.
        self._turns.pop(session_id, None)
        self.private_mode.on_session_end(session_id)

    def _pre_gateway_dispatch(self, event: Any = None, **kwargs: Any) -> Optional[dict]:
        """A new gateway message arrived. Convert to RawEvent and queue it.

        Returns None (or {"action": "allow"}) so the dispatch continues
        normally. We do not block the message; we just shadow-log it.
        """
        try:
            raw = raw_event_from_message(event)
            # Stash it on the turn that's about to start so pre_llm_call
            # can find it. Keyed by session_id from the event.
            session_id = getattr(event, "session_id", "") or getattr(event, "chat_id", "") or ""
            turn = self._turns.setdefault(str(session_id), TurnContext())
            turn.raw_event = raw
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to convert gateway event to RawEvent: %s", e)
        return None

    def _pre_llm_call(self, session_id: str = "", messages: Optional[list] = None, **kwargs: Any) -> None:
        """Pre-LLM hook. This is where the brain *thinks before it speaks*.

        Inline mode (default): drives the 10-stage pipeline via
        ``solomon.pipeline.runner.run`` and populates ``TurnContext``
        from the events row that comes back. Audit verdict drives an
        optional system-message injection into ``messages``.

        Queue mode: inserts the events row with ``status='pending'``
        and returns. The pipeline-tick worker (out of scope, Session
        post-B) picks it up.

        Kill-switch: ``SOLOMON_PIPELINE_DISABLE=1`` runs the legacy
        inline 7-step body unchanged.

        Pipeline crash safety: any exception in the pipeline path is
        caught, the events row gets ``status='errored'``, and we fall
        through to the legacy body. Better degraded than dead.

        On private mode, we skip all of this and pass through silently.
        """
        if self.private_mode.is_active(session_id):
            return

        turn = self._turns.setdefault(str(session_id), TurnContext())
        turn.started_at = time.time()
        turn.private = False

        # If we didn't get a RawEvent from pre_gateway_dispatch (CLI session,
        # direct hermes chat -q invocation, etc.), synthesize one from the
        # last user message.
        if turn.raw_event is None and messages:
            turn.raw_event = self._synthesize_raw_event_from_messages(messages, session_id)

        if turn.raw_event is None:
            return  # nothing to score

        # Kill switch — full legacy path, no pipeline involvement.
        if _pipeline_disabled():
            logger.debug("SOLOMON_PIPELINE_DISABLE set; using legacy _pre_llm_call body")
            return self._pre_llm_call_legacy(turn)

        # Pipeline path. Everything is wrapped so a pipeline crash falls
        # through to the legacy body instead of crashing the turn.
        event_id: Optional[str] = None
        try:
            mode = _pipeline_mode()
            event_id = self._insert_pending_event(turn)
            turn.event_id = event_id

            if mode == "queue":
                # Out of scope this session: the pipeline-tick worker (TODO:
                # solomon/workers/pipeline_tick/__main__.py) will pick up
                # this row and run the 10 stages. Return now; the gateway
                # response goes out immediately, the shadow decision is
                # processed behind.
                logger.debug("pipeline queue-mode: event_id=%s queued", event_id)
                return

            # Inline mode — run the 10-stage pipeline now.
            from .pipeline.runner import run as run_pipeline
            run_pipeline(event_id)

            # Read the row back and populate TurnContext.
            row = self._read_event_row(event_id)
            if row is None:
                logger.warning(
                    "pipeline inline: events row %s vanished after run; "
                    "falling through to legacy", event_id,
                )
                return self._pre_llm_call_legacy(turn)

            self._populate_turn_from_row(turn, row)
            self._maybe_inject_system_message(messages, turn)
            return

        except Exception as e:  # noqa: BLE001
            logger.exception(
                "Conductor pipeline path failed (event_id=%s); marking errored "
                "and falling through to legacy: %s", event_id, e,
            )
            if event_id is not None:
                try:
                    self._mark_event_errored(event_id, str(e))
                except Exception as nested:  # noqa: BLE001
                    logger.warning(
                        "Could not mark event %s as errored: %s", event_id, nested,
                    )
            # Legacy fall-through so the user still gets a response.
            return self._pre_llm_call_legacy(turn)

    # -- pipeline helpers ---------------------------------------------------

    @staticmethod
    def _new_event_id() -> str:
        """Generate a ulid-style id. We don't depend on ulid-py here —
        ``uuid4().hex`` gives us the same shape (32 hex chars, globally
        unique) without an extra dep. The corpus pipeline does the same.
        """
        return uuid.uuid4().hex

    def _insert_pending_event(self, turn: TurnContext) -> str:
        """INSERT a pending events row for ``turn.raw_event``. Returns
        the event_id. Uses the portable pool API (``?`` placeholders).

        We populate ``raw_content`` and ``channel_metadata`` from the
        RawEvent. The schema doesn't carry a separate ``payload`` JSON
        column on events — the raw turn payload lives in
        ``channel_metadata`` (the standard place for gateway-specific
        context) plus ``raw_content`` (the user-visible text).
        """
        from .storage.decisions import get_or_create_tenant_id
        from .storage.pool import cursor, execute, get_conn

        tenant_id = get_or_create_tenant_id()
        raw = turn.raw_event
        assert raw is not None  # caller guarantees

        event_id = self._new_event_id()
        received_at = raw.received_at
        if hasattr(received_at, "isoformat"):
            received_at = received_at.isoformat()

        # Build a payload that captures the raw turn data. Stored as
        # channel_metadata JSON to stay within the existing schema.
        payload = {
            "source": raw.source,
            "participants": raw.participants,
            "channel_metadata": raw.channel_metadata,
            "session_id": str(turn.raw_event.channel_metadata.get("session_id", "")) if turn.raw_event else "",
        }

        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "INSERT INTO events ("
                    "  event_id, tenant_id, source, received_at, participants, "
                    "  raw_content, channel_metadata, status, private"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        tenant_id,
                        raw.source,
                        received_at,
                        json.dumps(raw.participants),
                        raw.raw_content,
                        json.dumps(payload),
                        "pending",
                        0,
                    ),
                )
            conn.commit()
        return event_id

    @staticmethod
    def _read_event_row(event_id: str) -> Optional[Dict[str, Any]]:
        """SELECT * FROM events WHERE event_id=? → dict. None if missing."""
        from .storage.pool import cursor, execute, get_conn, row_to_dict
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(cur, "SELECT * FROM events WHERE event_id = ? LIMIT 1", (event_id,))
                row = cur.fetchone()
                if row is None:
                    return None
                return row_to_dict(row)

    @staticmethod
    def _mark_event_errored(event_id: str, reason: str) -> None:
        """Set status='errored' on the events row. Best effort — we
        already swallowed the original exception, so a second failure
        here is logged but doesn't propagate.
        """
        from .storage.pool import cursor, execute, get_conn
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "UPDATE events SET status = ?, audit_reasoning = ? "
                    "WHERE event_id = ?",
                    ("errored", f"pipeline error: {reason}"[:500], event_id),
                )
            conn.commit()

    def _populate_turn_from_row(self, turn: TurnContext, row: Dict[str, Any]) -> None:
        """Copy the 14 columns the prompt calls for from ``row`` onto
        ``turn``. JSON columns are parsed back into dicts. The legacy
        flat fields (``scope``, ``salience_breakdown`` etc.) are
        derived where they map cleanly so downstream code still works.
        """
        from .storage.pool import parse_json

        # Direct mirrors.
        turn.event_id = row.get("event_id")
        turn.status = row.get("status")
        turn.action_taken = row.get("action_taken")
        turn.audit_verdict = row.get("audit_verdict")
        turn.audit_reasoning = row.get("audit_reasoning")
        turn.owner_state = row.get("owner_state")
        turn.owner_state_ceiling = row.get("owner_state_ceiling")
        turn.effective_autonomy = row.get("effective_autonomy")

        sal = row.get("salience_score")
        if sal is not None:
            try:
                turn.salience_score = float(sal)
            except (TypeError, ValueError):
                pass

        div = row.get("divergence_score")
        if div is not None:
            try:
                turn.divergence_score = float(div)
            except (TypeError, ValueError):
                pass

        # JSON columns → dicts/objects.
        classification = parse_json(row.get("classification"))
        if isinstance(classification, dict):
            turn.classification = classification
            turn.scope = classification.get("scope")
            turn.domain = classification.get("domain")
            turn.decision_type = classification.get("decision_type")
            conf = classification.get("confidence")
            try:
                turn.classification_confidence = float(conf) if conf is not None else 0.0
            except (TypeError, ValueError):
                turn.classification_confidence = 0.0
        else:
            turn.classification = None

        turn.system1_output = parse_json(row.get("system1_output")) or row.get("system1_output")
        turn.system2_output = parse_json(row.get("system2_output")) or row.get("system2_output")
        # Best-effort legacy mirror so older log/storage paths still get
        # a useful string.
        if isinstance(turn.system2_output, dict):
            turn.system_2_answer = (
                turn.system2_output.get("reasoning")
                or turn.system2_output.get("proposed_action")
            )
            turn.proposed_action = (
                turn.system2_output.get("proposed_action")
                or turn.proposed_action
            )
        elif isinstance(turn.system2_output, str):
            turn.system_2_answer = turn.system2_output

        if isinstance(turn.system1_output, dict):
            turn.system_1_answer = (
                turn.system1_output.get("answer")
                or turn.system1_output.get("reasoning")
            )
        elif isinstance(turn.system1_output, str):
            turn.system_1_answer = turn.system1_output

        timings = parse_json(row.get("stage_timings_ms"))
        if isinstance(timings, dict):
            turn.stage_timings_ms = timings
        else:
            turn.stage_timings_ms = {}

        # autonomy_level_at_time mirrors effective_autonomy as L<n>.
        eff = turn.effective_autonomy
        if eff is not None:
            try:
                turn.autonomy_level_at_time = f"L{int(eff)}"
            except (TypeError, ValueError):
                pass

    @staticmethod
    def _maybe_inject_system_message(messages: Optional[list], turn: TurnContext) -> None:
        """Append a system message to ``messages`` based on the pipeline
        verdict, per the audit-verdict mapping in the module docstring.

        ``messages`` is the list that's about to be sent to the LLM.
        Lists are mutable, so appending here is what Hermes sees.

        We do NOT touch ``TurnContext.system_prompt`` — that field is
        overwritten elsewhere in the Hermes pipeline and our injection
        would be lost.
        """
        if messages is None:
            return

        status = (turn.status or "").strip().lower()
        verdict = (turn.audit_verdict or "").strip().upper()

        msg: Optional[str] = None
        if status == "skipped":
            # Low salience — nothing to add.
            return
        elif status == "blocked_by_hard_rule":
            reason = turn.audit_reasoning or "this request violates one of the owner's non-negotiables"
            msg = (
                "[Solomon pipeline] This request was blocked by a hard rule "
                "(non-negotiable). Decline the request and briefly explain that "
                f"it violates a non-negotiable: {reason}. Do not attempt the action."
            )
        elif status == "complete" and verdict == "REJECT":
            reason = turn.audit_reasoning or "the audit gate rejected the proposed action"
            msg = (
                "[Solomon pipeline] The audit gate REJECTED the proposed action. "
                f"Reasoning: {reason}. Decline the request and explain the audit's reasoning."
            )
        elif status == "complete" and verdict == "REQUEST_RETHINK":
            reason = turn.audit_reasoning or "the audit gate asked for a second look"
            msg = (
                "[Solomon pipeline] The audit gate asked for a RETHINK. "
                f"Reasoning: {reason}. Reconsider your reply with this concern in mind "
                "before answering the owner."
            )
        elif status == "complete" and verdict == "APPROVE":
            # Approved — nothing to inject.
            return
        else:
            # Unknown / errored / pending — be conservative, inject nothing.
            return

        try:
            messages.append({"role": "system", "content": msg})
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not append pipeline system message: %s", e)

    # -- legacy pre_llm_call (used as fallback) ----------------------------

    def _pre_llm_call_legacy(self, turn: TurnContext) -> None:
        """The pre-pipeline body. Kept verbatim so the kill-switch and the
        try/except fallback produce bit-for-bit the old behaviour.

        Steps 1–8 from Part 4 of the design doc happen here. By the time the
        actual LLM call goes out, salience, classification, retrieval, S1,
        S2, and the audit verdict are already computed and attached to the
        turn context.
        """
        if turn.raw_event is None:
            return  # nothing to score

        try:
            # Step 1 — Classify
            cls = self.classifier.classify(turn.raw_event)
            turn.scope, turn.domain, turn.decision_type = cls.scope, cls.domain, cls.decision_type
            turn.classification_confidence = cls.confidence

            # Step 2 — Salience
            sal = self.salience.score(turn.raw_event, scope=turn.scope)
            turn.salience_score = sal.score
            turn.salience_breakdown = sal.breakdown

            # Step 3 — Non-negotiable check (runs even in private mode; the
            # private check happened earlier, this is reached only on
            # non-private turns)
            violation = self.non_negotiables.check(turn.raw_event, scope=turn.scope)
            if violation:
                logger.warning("Non-negotiable violation detected: %s", violation.reason)
                # The conductor doesn't act here — it logs and lets the
                # audit gate decide. Escalation happens through normal
                # Hermes approval flow.
                turn.audit_verdict = "reject"
                turn.audit_reasoning = f"non-negotiable: {violation.reason}"
                return

            # Steps 4–5 — Memory retrieval
            hot = self.working_memory.fetch(scope=turn.scope, raw_event=turn.raw_event)
            turn.working_memory_used = not hot.is_thin()
            if hot.is_thin():
                long_ctx = self.retrieval.retrieve(turn.raw_event, scope=turn.scope, domain=turn.domain)
                turn.retrieval_lanes_used = long_ctx.lanes
                turn.heuristics_referenced = long_ctx.heuristic_ids
                turn.foundation_files_used = long_ctx.foundation_files

            # Steps 6–7 — Predict, then reason
            s1 = self.s1.predict(turn.raw_event, scope=turn.scope, heuristics=turn.heuristics_referenced)
            turn.system_1_answer = s1.answer
            s2 = self.s2.reason(turn.raw_event, scope=turn.scope, context=hot, heuristic_ids=turn.heuristics_referenced)
            turn.system_2_answer = s2.answer
            turn.proposed_action = s2.proposed_action

            # Step 8 — Surprise score
            turn.divergence_score = self._divergence(s1.answer, s2.answer)

            # Step 9 — Autonomy lookup
            turn.autonomy_level_at_time = self.autonomy.level_for(turn.scope)

            # Step 10 — Audit gate
            verdict = self.audit_gate.run(
                proposed_action=turn.proposed_action,
                context=hot,
                surprise=turn.divergence_score,
                scope=turn.scope,
            )
            turn.audit_verdict = verdict.verdict
            turn.audit_reasoning = verdict.reasoning

        except Exception as e:  # noqa: BLE001
            # Conductor failures must never crash Hermes. Log, mark the
            # turn as degraded, and let the LLM call proceed normally.
            logger.exception("Conductor pre_llm_call (legacy) failed: %s", e)
            turn.audit_verdict = "degraded"
            turn.audit_reasoning = f"conductor error: {e}"

    def _post_llm_call(self, session_id: str = "", response: Any = None, **kwargs: Any) -> None:
        """After the LLM responded. Finalize the decision, log it, schedule
        predictions and counterfactuals. Skip if private mode.

        New (Session B): if the turn carries an ``event_id`` (i.e. the
        pipeline path ran), call ``mirror_event_to_decision`` so the
        sleep-cycle / review-queue consumers see the row. The mirror is
        idempotent — it no-ops if ``stage_action`` already mirrored.
        """
        if self.private_mode.is_active(session_id):
            self.private_mode.record_private_turn(session_id)
            return

        turn = self._turns.get(str(session_id))
        if turn is None or turn.raw_event is None:
            return

        # Capture the actual response. This may differ from proposed_action
        # if the audit gate downgraded or the model edited mid-stream.
        turn.final_action = self._extract_action_text(response)

        # Pipeline path: mirror the events row into decisions for the
        # H2 audit log + downstream sleep-cycle / review-queue consumers.
        # Idempotent — stage_action may have already done this in inline
        # mode; in queue mode the worker will do it later.
        if turn.event_id:
            try:
                from .storage.decisions import mirror_event_to_decision
                mirror_event_to_decision(turn.event_id)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "post_llm_call: mirror_event_to_decision(%s) failed: %s",
                    turn.event_id, e,
                )

        try:
            decision_id = self.decision_log.log(turn)
            if turn.salience_score >= 0.4 and turn.system_2_answer:
                # Store a checkpoint prediction and a counterfactual for
                # high-salience decisions.
                self.predictions.store_for_decision(decision_id, turn)
                self.counterfactuals.store_for_decision(decision_id, turn)
            self.working_memory.update_after_turn(turn)
        except Exception as e:  # noqa: BLE001
            logger.exception("Conductor post_llm_call logging failed: %s", e)

        # Drop the turn — keeping it around would leak memory across long
        # sessions.
        self._turns.pop(str(session_id), None)

    def _pre_tool_call(self, tool_name: str = "", args: Optional[dict] = None, session_id: str = "", **kwargs: Any) -> Optional[dict]:
        """A tool is about to run. The audit gate already approved the
        overall turn; here we just record that this specific action is
        firing. We do NOT block tools — that's the autonomy ladder's job
        upstream.
        """
        if self.private_mode.is_active(session_id):
            return None
        # Lightweight: just note it on the turn context for later logging.
        turn = self._turns.get(str(session_id))
        if turn is not None:
            turn.proposed_action = f"tool:{tool_name}"
        return None

    def _post_tool_call(self, tool_name: str = "", result: Any = None, session_id: str = "", **kwargs: Any) -> None:
        """Tool finished. We could log per-tool outcomes here, but for
        now decision-level logging in _post_llm_call is enough.
        """
        return None

    # -- helpers ------------------------------------------------------------

    def _synthesize_raw_event_from_messages(self, messages: list, session_id: str) -> Optional[RawEvent]:
        """When there's no gateway event (CLI invocation), build a RawEvent
        from the last user message so the brain still has something to
        score and classify.
        """
        try:
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    return RawEvent(
                        id=f"cli:{session_id}:{int(time.time() * 1000)}",
                        source="cli",
                        received_at=datetime.now(timezone.utc),
                        participants=[],
                        raw_content=str(msg.get("content", "")),
                        channel_metadata={"session_id": session_id},
                    )
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not synthesize RawEvent from messages: %s", e)
        return None

    @staticmethod
    def _extract_action_text(response: Any) -> str:
        """Best-effort extraction of the final assistant text from whatever
        Hermes hands us as a response. Hermes can change shape here over
        time; we keep this defensive.
        """
        if response is None:
            return ""
        if isinstance(response, str):
            return response
        # OpenAI-style choices
        try:
            return str(response.choices[0].message.content or "")
        except Exception:  # noqa: BLE001
            pass
        # dict with 'content' key
        if isinstance(response, dict):
            return str(response.get("content", "") or response.get("final_response", "") or "")
        return str(response)

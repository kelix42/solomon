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
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .adapter import HermesAdapter
from .capture.raw_event import RawEvent, raw_event_from_message
from .private.mode import PrivateMode

logger = logging.getLogger("solomon.conductor")


@dataclass
class TurnContext:
    """Everything the conductor accumulates as a turn flows through it.

    Created at pre_llm_call, populated step by step, finalized at
    post_llm_call. Then handed to log_decision before being discarded.
    """
    raw_event: Optional[RawEvent] = None
    scope: Optional[str] = None
    domain: Optional[str] = None
    decision_type: Optional[str] = None
    classification_confidence: float = 0.0
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

        Steps 1–8 from Part 4 of the design doc happen here. By the time the
        actual LLM call goes out, salience, classification, retrieval, S1,
        S2, and the audit verdict are already computed and attached to the
        turn context.

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
            logger.exception("Conductor pre_llm_call failed: %s", e)
            turn.audit_verdict = "degraded"
            turn.audit_reasoning = f"conductor error: {e}"

    def _post_llm_call(self, session_id: str = "", response: Any = None, **kwargs: Any) -> None:
        """After the LLM responded. Finalize the decision, log it, schedule
        predictions and counterfactuals. Skip if private mode.
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

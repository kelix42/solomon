"""Part 10: The audit gate.

The audit gate is a separate Claude call (DEEP tier, Opus) that audits every
proposed action *before* it ships. It is the last cheap line of defense
between Solomon's reasoning layer and the outside world.

It checks five things:

  1. Hard-rule: does the action violate a non-negotiable? → REJECT.
  2. Confidence: is it high enough for the current autonomy level?
     If not → DOWNGRADE (e.g. ship-it becomes a suggestion).
  3. Scope: is it inside the authorized bounds? If not → DOWNGRADE or REJECT.
  4. Coherence: does it line up with the foundation principles and recent
     decisions? If not → DOWNGRADE.
  5. Tone: does it sound like the owner? If not → DOWNGRADE.

The gate returns one of four verdicts: APPROVE, DOWNGRADE, REJECT,
REQUEST_RETHINK. Anything else (or a transport failure) defaults to
DOWNGRADE — it is always safer to demote a confident action to a
suggestion than to ship something we couldn't audit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

from ..reasoning.llm import get_client

logger = logging.getLogger("solomon.audit_gate")


_ALLOWED_VERDICTS = {"approve", "downgrade", "reject", "request_rethink"}


@dataclass
class AuditVerdict:
    """Structured result of one audit gate pass."""

    verdict: str
    reasoning: str
    checks_passed: List[str] = field(default_factory=list)
    checks_failed: List[str] = field(default_factory=list)


class AuditGate:
    """Audit gate (Part 10). One Opus call per proposed action."""

    def __init__(self, adapter) -> None:  # noqa: ANN001
        self.adapter = adapter

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _summarize_context(self, context: Any) -> str:
        """Render HotContext as a single line for the audit prompt."""
        if context is None:
            return "context: 0 recent items, top scope=none"
        items = getattr(context, "items", None) or []
        n = len(items)
        top_scope = "none"
        if items:
            try:
                top_scope = str(items[0].get("scope") or "none")
            except Exception:  # noqa: BLE001
                top_scope = "none"
        return f"context: {n} recent items, top scope={top_scope}"

    # ------------------------------------------------------------------
    # Main entrypoint
    # ------------------------------------------------------------------
    def run(
        self,
        proposed_action: str,
        context: Any,
        surprise: float,
        scope: Optional[str],
    ) -> AuditVerdict:
        """Audit `proposed_action`. Never raises into the conductor."""

        # Nothing proposed → nothing to audit. Auto-approve.
        if not proposed_action:
            return AuditVerdict(
                verdict="approve",
                reasoning="no action proposed",
                checks_passed=[],
                checks_failed=[],
            )

        client = get_client()
        if not client.configured:
            logger.info("AuditGate: LLM unconfigured; defaulting to downgrade.")
            return AuditVerdict(
                verdict="downgrade",
                reasoning="audit gate unavailable, defaulting to suggest mode",
                checks_passed=[],
                checks_failed=[],
            )

        context_line = self._summarize_context(context)
        scope_line = f"scope={scope}" if scope else "scope=unspecified"
        try:
            surprise_val = float(surprise)
        except (TypeError, ValueError):
            surprise_val = 0.0

        system_prompt = (
            "You are the audit gate for Solomon, an AI decision engine. "
            "Check proposed actions before they ship. Return JSON: "
            "{verdict: approve|downgrade|reject|request_rethink, "
            "reasoning: str, checks_passed: list, checks_failed: list}."
        )
        user_prompt = (
            f"Proposed action:\n{proposed_action}\n\n"
            f"{context_line}\n"
            f"{scope_line}\n"
            f"surprise={surprise_val:.2f}\n\n"
            "Run the five checks (hard-rule, confidence, scope, coherence, "
            "tone) and respond with the JSON object now."
        )

        try:
            resp = client.call(
                tier="deep",
                system=system_prompt,
                user=user_prompt,
                json_mode=True,
                max_tokens=512,
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("AuditGate: LLM call raised: %s", e)
            return AuditVerdict(
                verdict="downgrade",
                reasoning="audit gate unavailable, defaulting to suggest mode",
                checks_passed=[],
                checks_failed=[],
            )

        text = (resp.text or "").strip()
        if not text:
            logger.info("AuditGate: empty response; downgrading.")
            return AuditVerdict(
                verdict="downgrade",
                reasoning="audit gate unavailable, defaulting to suggest mode",
                checks_passed=[],
                checks_failed=[],
            )

        parsed = client.parse_json(text)
        if not parsed or not isinstance(parsed, dict):
            logger.info("AuditGate: JSON parse failed; downgrading.")
            return AuditVerdict(
                verdict="downgrade",
                reasoning="audit gate unavailable, defaulting to suggest mode",
                checks_passed=[],
                checks_failed=[],
            )

        raw_verdict = str(parsed.get("verdict") or "").strip().lower()
        if raw_verdict not in _ALLOWED_VERDICTS:
            logger.info(
                "AuditGate: invalid verdict %r; downgrading.", raw_verdict
            )
            return AuditVerdict(
                verdict="downgrade",
                reasoning="audit gate unavailable, defaulting to suggest mode",
                checks_passed=[],
                checks_failed=[],
            )

        reasoning = str(parsed.get("reasoning") or "").strip()

        passed_raw = parsed.get("checks_passed") or []
        failed_raw = parsed.get("checks_failed") or []
        checks_passed: List[str] = (
            [str(x) for x in passed_raw] if isinstance(passed_raw, list) else []
        )
        checks_failed: List[str] = (
            [str(x) for x in failed_raw] if isinstance(failed_raw, list) else []
        )

        return AuditVerdict(
            verdict=raw_verdict,
            reasoning=reasoning,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
        )

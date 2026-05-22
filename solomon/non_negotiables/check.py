"""Non-negotiable checker (thin shim over the JSON-logic hard-rule engine).

REPORT-PIPELINE.md §3 row "JSON-logic non-negotiables" replaced our old
``keyword`` / ``regex`` modes with structured JSON-logic. This module
used to carry its own per-type evaluation; now it's a synchronous
wrapper around ``solomon.pipeline.stage_hard_rule.evaluate_rules`` so
callers outside the pipeline (notably ``solomon.conductor`` until
session B rewires it) still get the same dataclass back.

Public surface kept stable so ``solomon/conductor.py`` doesn't have to
change this session:

  * ``NonNegotiableViolation`` (dataclass) — same fields as before.
  * ``NonNegotiableChecker(adapter)`` — class wrapping the rules.
  * ``.check(raw_event, scope=None) -> Optional[NonNegotiableViolation]``.

One escape hatch is preserved: rules with ``check_type: 'llm'`` (or
``check_type: 'fuzzy'``) fall through to a one-shot tier="fast" LLM
call rather than JSON-logic. This is the single concession for fuzzy
rules that can't be expressed structurally. Legacy ``keyword`` /
``regex`` rules are dropped (REPORT-PIPELINE.md §3: "subsumed by
JSON-logic + an `llm` escape hatch").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..pipeline.stage_hard_rule import _load_rules, evaluate_rules
from ..reasoning.llm import get_client

logger = logging.getLogger("solomon.non_negotiables")


@dataclass
class NonNegotiableViolation:
    rule_name: str
    reason: str
    scope: Optional[str] = None


class NonNegotiableChecker:
    """Evaluates the JSON-logic rules from foundation/05-non-negotiables.yaml.

    Loads rules once at construction time (cheap YAML read + parse).
    Returns the first violation, or ``None`` if the event is clean. Never
    raises into the caller — a corrupt rule file degrades to "no rules
    loaded" (i.e. always pass), which is the conductor's expectation per
    Part 6 of the design doc.
    """

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.rules: List[Dict[str, Any]] = _load_rules()
        if self.rules:
            logger.info("Loaded %d non-negotiable rule(s).", len(self.rules))

    # ------------------------------------------------------------------
    # Main entrypoint
    # ------------------------------------------------------------------
    def check(
        self,
        raw_event: Any,
        scope: Optional[str] = None,
    ) -> Optional[NonNegotiableViolation]:
        """Return the first violation found, or None if the event is clean.

        Scope filtering: rules with a matching ``scope`` (or no scope, or
        ``scope: 'all'``) are evaluated. Other rules are skipped.
        """
        if not self.rules:
            return None

        applicable = [r for r in self.rules if self._scope_matches(r, scope)]
        if not applicable:
            return None

        # Build the json_logic ``data`` shape exactly like stage_hard_rule
        # does, so JSON-logic conditions written against
        # ``var: event.classification.scope`` work both in-pipeline and
        # here.
        data = {"event": self._render_event(raw_event, scope)}

        # First: split into JSON-logic rules and LLM-escape-hatch rules.
        # JSON-logic rules go through evaluate_rules; LLM rules are
        # evaluated one-by-one with the fast tier.
        jsonlogic_rules: List[Dict[str, Any]] = []
        llm_rules: List[Dict[str, Any]] = []
        for r in applicable:
            ctype = (r.get("check_type") or "").strip().lower()
            if ctype in {"llm", "fuzzy"}:
                llm_rules.append(r)
            else:
                # Default: JSON-logic. The presence of a ``condition`` key
                # is enough to qualify.
                jsonlogic_rules.append(r)

        matched = evaluate_rules(jsonlogic_rules, data) if jsonlogic_rules else None
        if matched is not None:
            return self._violation_from(matched, scope, reason_default="hard rule matched")

        # Then walk the LLM-escape-hatch rules.
        content = self._extract_content(raw_event)
        for r in llm_rules:
            try:
                hit = self._check_llm(r, content)
            except Exception as e:  # noqa: BLE001
                logger.warning("LLM non-negotiable %r raised: %s", r.get("name") or r.get("id"), e)
                continue
            if hit is not None:
                return hit

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _scope_matches(rule: Dict[str, Any], scope: Optional[str]) -> bool:
        rule_scope = rule.get("scope")
        if not rule_scope or rule_scope == "all":
            return True
        if scope is None:
            return True
        return rule_scope == scope

    @staticmethod
    def _render_event(raw_event: Any, scope: Optional[str]) -> Dict[str, Any]:
        """Translate the runtime raw_event into the JSON-logic-friendly dict
        shape (so the same rule file works for in-pipeline use and here)."""
        out: Dict[str, Any] = {}
        if raw_event is None:
            out["raw_content"] = ""
            out["classification"] = {"scope": scope}
            return out
        if isinstance(raw_event, dict):
            out.update(raw_event)
            if "classification" not in out:
                out["classification"] = {"scope": scope}
            return out
        # Object-style raw_event (capture.raw_event.RawEvent).
        out["raw_content"] = getattr(raw_event, "raw_content", "") or ""
        out["source"] = getattr(raw_event, "source", "")
        out["participants"] = list(getattr(raw_event, "participants", []) or [])
        out["channel_metadata"] = dict(getattr(raw_event, "channel_metadata", {}) or {})
        out["classification"] = {"scope": scope}
        return out

    @staticmethod
    def _extract_content(raw_event: Any) -> str:
        if raw_event is None:
            return ""
        if isinstance(raw_event, dict):
            return str(raw_event.get("raw_content") or raw_event.get("content") or "")
        return str(getattr(raw_event, "raw_content", "") or getattr(raw_event, "content", "") or "")

    @staticmethod
    def _violation_from(
        rule: Dict[str, Any],
        scope: Optional[str],
        reason_default: str,
    ) -> NonNegotiableViolation:
        name = rule.get("name") or rule.get("id") or "<anonymous>"
        on_violate = rule.get("on_violate") or {}
        reason = (
            on_violate.get("explanation")
            or rule.get("statement")
            or rule.get("description")
            or reason_default
        )
        rule_scope = rule.get("scope")
        return NonNegotiableViolation(
            rule_name=str(name),
            reason=str(reason),
            scope=str(rule_scope) if rule_scope and rule_scope != "all" else None,
        )

    @staticmethod
    def _check_llm(rule: Dict[str, Any], content: str) -> Optional[NonNegotiableViolation]:
        """One-shot LLM call for fuzzy rules. tier='fast' (per session prompt)."""
        pattern = rule.get("check_pattern") or rule.get("statement") or rule.get("description") or ""
        if not pattern:
            return None
        client = get_client()
        if not client.configured:
            logger.info("LLM non-negotiable skipped: client not configured.")
            return None

        user_prompt = (
            f"{pattern}\n\n"
            "Event content:\n"
            f"---\n{content}\n---\n\n"
            'Does this event suggest the rule will be violated? '
            'Respond JSON {"violation": true|false, "reason": str}'
        )
        resp = client.call(
            tier="fast",
            system=(
                "You are a safety check for a personal AI assistant. "
                "Evaluate whether an incoming event would cause a hard rule "
                "to be violated. Be conservative — only flag clear violations. "
                "Always respond with valid JSON."
            ),
            user=user_prompt,
            json_mode=True,
            max_tokens=256,
            temperature=0.0,
        )
        parsed = client.parse_json(resp.text)
        if not parsed or not isinstance(parsed, dict):
            return None
        if not bool(parsed.get("violation")):
            return None
        reason = str(parsed.get("reason") or rule.get("description") or "LLM flagged violation")
        name = rule.get("name") or rule.get("id") or "<anonymous>"
        rule_scope = rule.get("scope")
        return NonNegotiableViolation(
            rule_name=str(name),
            reason=reason,
            scope=str(rule_scope) if rule_scope and rule_scope != "all" else None,
        )

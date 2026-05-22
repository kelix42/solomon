"""Classifier — assign scope/domain/decision_type to a RawEvent.

See Part 5 of the design doc.

The classifier is the second step in the capture pipeline (right after
salience scoring). For every event Solomon decides to keep, we ask the
fast LLM: *what kind of decision is this?* The answer is three short
tags:

  * ``scope``         — the broad bucket (pricing, hiring, scheduling,
                        vendor relations, complaints, strategy, …). Drawn
                        from a fixed taxonomy that the business owner
                        curates over time at mentoring sessions.
  * ``domain``        — a narrower label inside the scope (optional,
                        empty by default until the owner starts naming
                        sub-areas like 'pricing/wholesale' or
                        'hiring/line-cook').
  * ``decision_type`` — what *shape* of decision it is: a quote, a
                        complaint, a scheduling change, a strategic
                        choice, a policy question, etc.

These tags feed clustering, retrieval, and the audit gate. They are
intentionally cheap (one fast-tier LLM call, JSON mode) and forgiving
(low confidence collapses to 'general'; unknown tags get logged for
review instead of silently expanding the taxonomy).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from ..reasoning.llm import get_client

logger = logging.getLogger("solomon.classify")


DEFAULT_SCOPES: List[str] = [
    "pricing",
    "hiring",
    "scheduling",
    "vendor",
    "complaints",
    "strategy",
    "operations",
    "finance",
    "marketing",
    "sales",
    "general",
]

DEFAULT_DOMAINS: List[str] = []

# Confidence floor — anything below this and we refuse to commit to a
# specific scope. Keeps noisy events from polluting the cluster store.
MIN_CONFIDENCE: float = 0.5


@dataclass
class ClassificationResult:
    scope: Optional[str] = None
    domain: Optional[str] = None
    decision_type: Optional[str] = None
    confidence: float = 0.0


class Classifier:
    """Classify RawEvents into the owner's taxonomy.

    One classifier per Solomon process. Holds a reference to the
    PluginAdapter so it can read the live taxonomy from config and
    write warnings to the Hermes logger when the LLM hallucinates a
    new tag.
    """

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter
        self._llm = get_client()

    # -- public API ---------------------------------------------------------

    def classify(self, raw_event: Any) -> ClassificationResult:
        """Run one fast-tier LLM call and return a ClassificationResult.

        Never raises. On any failure (LLM down, bad JSON, etc.) the
        result is ('general', None, None, 0.0).
        """
        scopes = self._scopes()
        domains = self._domains()

        system = self._build_system_prompt(scopes, domains)
        user = self._build_user_prompt(raw_event)

        resp = self._llm.call(
            tier="fast",
            system=system,
            user=user,
            json_mode=True,
            max_tokens=256,
            temperature=0.1,
        )
        if not resp.text:
            logger.debug("Classifier got empty LLM response for event %s", _safe_id(raw_event))
            return ClassificationResult(scope="general", domain=None, decision_type=None, confidence=0.0)

        parsed = self._llm.parse_json(resp.text)
        if not isinstance(parsed, dict):
            logger.warning(
                "Classifier could not parse LLM JSON for event %s; raw=%r",
                _safe_id(raw_event),
                resp.text[:200],
            )
            return ClassificationResult(scope="general", domain=None, decision_type=None, confidence=0.0)

        return self._normalize(parsed, scopes, domains, raw_event)

    # -- helpers ------------------------------------------------------------

    def _scopes(self) -> List[str]:
        val = self._adapter.get_config("solomon.taxonomy.scopes", DEFAULT_SCOPES)
        if isinstance(val, list) and val:
            return [str(s).strip().lower() for s in val if str(s).strip()]
        return list(DEFAULT_SCOPES)

    def _domains(self) -> List[str]:
        val = self._adapter.get_config("solomon.taxonomy.domains", DEFAULT_DOMAINS)
        if isinstance(val, list):
            return [str(s).strip().lower() for s in val if str(s).strip()]
        return list(DEFAULT_DOMAINS)

    def _build_system_prompt(self, scopes: List[str], domains: List[str]) -> str:
        scope_str = ", ".join(scopes) if scopes else "(none — use 'general')"
        domain_str = ", ".join(domains) if domains else "(none defined yet — leave domain empty)"
        return (
            "You classify incoming business events for a decision-learning "
            "assistant. You DO NOT invent new tags. If nothing fits, return "
            "scope='general'.\n\n"
            f"Allowed scopes: {scope_str}\n"
            f"Allowed domains: {domain_str}\n\n"
            "Decision types are free-form short noun phrases describing the "
            "shape of the decision: 'quote', 'complaint', 'scheduling_change', "
            "'strategic_choice', 'policy_question', 'vendor_negotiation', "
            "'hiring_decision', 'pricing_change', 'status_update', etc.\n\n"
            "If the event touches multiple scopes, pick the single most "
            "relevant one. If you are not confident (<0.5), still return the "
            "best guess scope but set confidence accordingly — the caller "
            "will fall back to 'general'.\n\n"
            "Respond with JSON ONLY in this exact shape:\n"
            '{"scope": "<one-of-allowed-or-general>", '
            '"domain": "<one-of-allowed-or-empty-string>", '
            '"decision_type": "<short_snake_case>", '
            '"confidence": <float 0.0-1.0>}'
        )

    def _build_user_prompt(self, raw_event: Any) -> str:
        source = getattr(raw_event, "source", "unknown")
        received_at = getattr(raw_event, "received_at", None)
        participants = getattr(raw_event, "participants", []) or []
        content = getattr(raw_event, "raw_content", "") or ""

        # Truncate to keep the fast-tier call cheap. Classification only
        # needs the gist, not the full thread.
        if len(content) > 4000:
            content = content[:4000] + "…[truncated]"

        ts = received_at.isoformat() if received_at is not None and hasattr(received_at, "isoformat") else str(received_at)
        parts = ", ".join(str(p) for p in participants) if participants else "(none)"

        return (
            "Classify this event.\n\n"
            f"Source: {source}\n"
            f"Received at: {ts}\n"
            f"Participants: {parts}\n"
            "---\n"
            f"{content}\n"
            "---"
        )

    def _normalize(
        self,
        parsed: dict,
        scopes: List[str],
        domains: List[str],
        raw_event: Any,
    ) -> ClassificationResult:
        scope_raw = _str_or_none(parsed.get("scope"))
        domain_raw = _str_or_none(parsed.get("domain"))
        decision_type = _str_or_none(parsed.get("decision_type"))
        confidence = _float_or_zero(parsed.get("confidence"))
        confidence = max(0.0, min(1.0, confidence))

        scope = scope_raw.lower() if scope_raw else None
        domain = domain_raw.lower() if domain_raw else None

        # Unknown scope → log warning for the next mentoring session, then
        # fall back to 'general'.
        if scope and scope not in scopes:
            self._warn_unknown_tag("scope", scope, raw_event)
            scope = "general"

        # Unknown domain → also collapse to None, but only warn if domains
        # are actually defined (otherwise we'd warn on every event).
        if domain and domains and domain not in domains:
            self._warn_unknown_tag("domain", domain, raw_event)
            domain = None
        elif domain and not domains:
            # No domain taxonomy yet — silently drop, this is expected.
            domain = None

        # Low confidence → collapse scope to 'general' but preserve the
        # decision_type and the (now smaller) confidence value so audit
        # downstream can see how unsure we were.
        if confidence < MIN_CONFIDENCE:
            scope = "general"
            domain = None

        return ClassificationResult(
            scope=scope or "general",
            domain=domain,
            decision_type=decision_type,
            confidence=confidence,
        )

    def _warn_unknown_tag(self, kind: str, value: str, raw_event: Any) -> None:
        """Log an unknown-tag suggestion to the Hermes logger so the owner
        can review it at the next mentoring session and decide whether
        to add it to the taxonomy.
        """
        msg = (
            "Solomon classifier suggested unknown %s tag %r for event %s. "
            "Review at next mentoring session before adding to taxonomy."
        )
        eid = _safe_id(raw_event)
        logger.warning(msg, kind, value, eid)
        try:
            hlog = self._adapter.hermes_logger()
            if hlog is not None and hlog is not logger:
                hlog.warning(msg, kind, value, eid)
        except Exception:  # noqa: BLE001
            # Never let logging break classification.
            pass


# -- module-level helpers ---------------------------------------------------


def _str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _float_or_zero(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _safe_id(raw_event: Any) -> str:
    try:
        return str(getattr(raw_event, "id", "<no-id>"))
    except Exception:  # noqa: BLE001
        return "<no-id>"

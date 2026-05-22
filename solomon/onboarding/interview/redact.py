"""Redact — thin wrapper around solomon.ingestion.sensitivity_filter.scrub.

Interview-phase PII pass. Runs before extraction so we never store SSNs,
phones, credit cards, or emails in captured_items.verbatim_phrase or the
LLM context window.

Citation: docs/REPORT-INTERVIEW.md §4.3 (bullet 6).
Drive source: skills/utilities/solomon-redact/SKILL.md.
"""

from __future__ import annotations

import logging
from typing import Tuple, List

from ...ingestion.sensitivity_filter import scrub as _scrub

logger = logging.getLogger("solomon.onboarding.redact")


def redact(text: str) -> str:
    """Return the PII-redacted version of the owner's turn text.

    On any error, returns the original text rather than dropping the turn —
    the conductor should never crash on the owner's own words.
    """
    if not text:
        return text
    try:
        result = _scrub(text, document_flagged_sensitive=False)
        if result.matches:
            logger.info("Redacted owner turn: patterns=%s", result.matches)
        return result.redacted_text
    except Exception as e:  # noqa: BLE001
        logger.warning("redact failed: %s; returning original text", e)
        return text


def redact_with_matches(text: str) -> Tuple[str, List[str]]:
    """Same as redact() but also returns the list of pattern names that fired.

    Useful when the caller wants to surface a one-liner to the owner
    ("That phone number was redacted from the saved transcript.").
    """
    if not text:
        return text, []
    try:
        result = _scrub(text, document_flagged_sensitive=False)
        return result.redacted_text, list(result.matches)
    except Exception as e:  # noqa: BLE001
        logger.warning("redact_with_matches failed: %s; returning original", e)
        return text, []

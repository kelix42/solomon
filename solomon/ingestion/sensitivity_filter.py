"""Sensitivity filter. Runs before embedding.

Two layers:
  1. PII redaction. Find and mask SSNs, passport numbers, credit cards,
     phone numbers, email addresses inside the text BEFORE the embedder
     and BEFORE the extractor see it. Replaced inline with placeholder
     tokens. The unmodified raw_content is still kept in `raw_events`
     for owner audit, but it does not enter retrieval.
  2. Owner-flagged sensitive documents. Skip processing entirely. The
     document stays in object storage but no embedding, no extraction,
     no heuristic mining.

PII patterns are conservative: we'd rather over-redact than leak. False
positives just show up as [REDACTED-EMAIL] in retrieval results, which
is annoying but not dangerous.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Tuple

logger = logging.getLogger("solomon.ingestion.sensitivity")


PATTERNS: List[Tuple[str, re.Pattern[str], str]] = [
    # US SSN — three-two-four
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED-SSN]"),
    # Canadian SIN — three-three-three
    ("SIN", re.compile(r"\b\d{3}\s\d{3}\s\d{3}\b"), "[REDACTED-SIN]"),
    # Credit card — generic 13-19 digit run
    ("CC", re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "[REDACTED-CC]"),
    # Phone — North American
    ("PHONE", re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[REDACTED-PHONE]"),
    # Passport — letter + 8 digits (loose; tightens vary per country)
    ("PASSPORT", re.compile(r"\b[A-Z]\d{8}\b"), "[REDACTED-PASSPORT]"),
    # Email
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[REDACTED-EMAIL]"),
]


@dataclass
class SensitivityResult:
    redacted_text: str
    matches: List[str]  # list of pattern names that matched
    skip_document: bool = False  # True if owner-flagged sensitive


def scrub(text: str, document_flagged_sensitive: bool = False) -> SensitivityResult:
    """Apply all PII patterns to the text. Return the redacted version
    plus the list of pattern names that fired (for the owner audit log).

    If `document_flagged_sensitive` is True, returns skip_document=True
    and an empty redacted_text — caller skips processing entirely.
    """
    if document_flagged_sensitive:
        return SensitivityResult(redacted_text="", matches=["FLAGGED_SENSITIVE"], skip_document=True)
    if not text:
        return SensitivityResult(redacted_text="", matches=[])

    matches: List[str] = []
    out = text
    for name, pattern, placeholder in PATTERNS:
        if pattern.search(out):
            matches.append(name)
            out = pattern.sub(placeholder, out)

    return SensitivityResult(redacted_text=out, matches=matches)


def scrub_batch(texts: List[str]) -> List[SensitivityResult]:
    return [scrub(t) for t in texts]

"""Sensitivity filter — spaCy NER + regex + allowlist + quarantine.

See REPORT-CORPUS.md §1.10 and §4.5. Ported from
/root/projects/solomon-from-drive/skills/utilities/solomon-redact/redactor.py
(adapted to keep our existing public API: ``scrub`` returning
``SensitivityResult``).

Two layers:
  1. spaCy NER for PERSON / ORG / LOC / GPE — masked as ``[REDACTED-ENTITY]``
     with owner allowlist (``corpus/schema.md::entity_allowlist``).
  2. Regex patterns: SSN, SIN, phone, credit card (Luhn-checked),
     AWS access keys (``AKIA...``), prefixed API keys / Bearer tokens,
     SSH PEM markers, labeled passwords, passport, email.

spaCy is a **soft dependency**: if not installed (or the en_core_web_sm
model is missing), we fall back to regex-only and log a warning.

Public API (unchanged for backwards compatibility with our tests):
  - ``scrub(text, document_flagged_sensitive=False) -> SensitivityResult``
  - ``scrub_batch(texts) -> List[SensitivityResult]``
  - ``PATTERNS`` (list of (name, compiled_regex, placeholder))
  - ``SensitivityResult`` dataclass
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger("solomon.ingestion.sensitivity")


# ---------------------------------------------------------------------------
# Regex patterns. Order matters: the engine iterates in declaration order and
# the credit-card placeholder must not eat a longer phone or SSN match. The
# CC pattern is Luhn-checked at runtime.
# ---------------------------------------------------------------------------

PATTERNS: List[Tuple[str, re.Pattern[str], str]] = [
    # US SSN — three-two-four
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED-SSN]"),
    # Canadian SIN — three-three-three
    ("SIN", re.compile(r"\b\d{3}\s\d{3}\s\d{3}\b"), "[REDACTED-SIN]"),
    # AWS access key — AKIA + 16 upper alnum
    ("AWS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED-AWS-KEY]"),
    # SSH private key PEM marker (whole line will look broken anyway after redact)
    ("SSH_KEY", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"), "[REDACTED-SSH-KEY]"),
    # Phone — North American
    ("PHONE", re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[REDACTED-PHONE]"),
    # Passport — letter + 8 digits (loose; per-country format varies)
    ("PASSPORT", re.compile(r"\b[A-Z]\d{8}\b"), "[REDACTED-PASSPORT]"),
    # Email
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[REDACTED-EMAIL]"),
    # Credit card — generic 13-19 digit run, Luhn-validated at runtime.
    ("CC", re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "[REDACTED-CC]"),
]

# Two patterns are handled out-of-band because they replace only the
# captured group, not the whole match.
_API_KEY_RE = re.compile(
    r"(Bearer\s+|api[_-]?key\s*[:=]\s*|token\s*[:=]\s*)([A-Za-z0-9_\-\.]{20,})",
    re.IGNORECASE,
)
_PASSWORD_RE = re.compile(
    r"((?:password|passwd|pwd)\s*[:=]\s*['\"]?)([^\s'\"\n]{6,})(['\"]?)",
    re.IGNORECASE,
)

_ENTITY_LABELS = {"PERSON", "ORG", "LOC", "GPE"}

# Lazy-loaded spaCy pipeline; ``False`` after a confirmed failed load so we
# don't retry every call.
_SPACY_NLP = None  # type: ignore[var-annotated]
_SPACY_LOCK = threading.Lock()


def _luhn_valid(digits: str) -> bool:
    cleaned = re.sub(r"[ -]", "", digits)
    if not cleaned.isdigit() or not (13 <= len(cleaned) <= 19):
        return False
    s = 0
    parity = len(cleaned) % 2
    for i, d in enumerate(cleaned):
        n = int(d)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        s += n
    return s % 10 == 0


def _load_spacy():  # type: ignore[no-untyped-def]
    """Try to load en_core_web_sm. Returns the pipeline or ``False`` if not
    available. Cached for the process lifetime.
    """
    global _SPACY_NLP
    if _SPACY_NLP is not None:
        return _SPACY_NLP
    with _SPACY_LOCK:
        if _SPACY_NLP is not None:
            return _SPACY_NLP
        try:
            import spacy  # type: ignore
        except ImportError:
            logger.info("spaCy not installed; redactor will use regex-only.")
            _SPACY_NLP = False  # type: ignore[assignment]
            return False
        try:
            _SPACY_NLP = spacy.load("en_core_web_sm")  # type: ignore[assignment]
        except OSError:
            logger.warning(
                "spaCy is installed but the en_core_web_sm model is missing. "
                "Run `python -m spacy download en_core_web_sm` or install the "
                "`redaction-spacy` extra. Falling back to regex-only redaction."
            )
            _SPACY_NLP = False  # type: ignore[assignment]
            return False
    return _SPACY_NLP


@dataclass
class SensitivityResult:
    redacted_text: str
    matches: List[str]  # pattern + entity-label names that fired
    skip_document: bool = False


def _apply_regex_patterns(text: str, matches: List[str]) -> str:
    out = text
    for name, pattern, placeholder in PATTERNS:
        if name == "CC":
            # Special-case: Luhn-check each candidate; sub only valid CCs.
            def _maybe_redact(m: re.Match[str]) -> str:
                return placeholder if _luhn_valid(m.group(0)) else m.group(0)

            new_out, n = pattern.subn(_maybe_redact, out)
            if n and new_out != out:
                matches.append(name)
            out = new_out
            continue
        if pattern.search(out):
            matches.append(name)
            out = pattern.sub(placeholder, out)
    # API-key / Bearer / token=...  — keep the label, replace the value.
    if _API_KEY_RE.search(out):
        matches.append("API_KEY")
        out = _API_KEY_RE.sub(lambda m: f"{m.group(1)}[REDACTED-API-KEY]", out)
    # password=... — keep the label and quotes, replace the value.
    if _PASSWORD_RE.search(out):
        matches.append("PASSWORD")
        out = _PASSWORD_RE.sub(lambda m: f"{m.group(1)}[REDACTED-PASSWORD]{m.group(3)}", out)
    return out


def _apply_entity_redaction(text: str, allowlist: List[str], matches: List[str]) -> str:
    nlp = _load_spacy()
    if not nlp:
        return text
    try:
        doc = nlp(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("spaCy NER failed at runtime: %s", e)
        return text
    # Collect spans (start_char, end_char, label) then apply in reverse.
    spans: List[Tuple[int, int, str]] = []
    allow_lower = [a.lower() for a in (allowlist or [])]
    for ent in doc.ents:
        if ent.label_ not in _ENTITY_LABELS:
            continue
        et = ent.text
        if any(a in et.lower() for a in allow_lower):
            continue
        spans.append((ent.start_char, ent.end_char, ent.label_))
    if not spans:
        return text
    matches.append("ENTITY")
    spans.sort(key=lambda s: s[0], reverse=True)
    out = text
    for start, end, _label in spans:
        out = out[:start] + "[REDACTED-ENTITY]" + out[end:]
    return out


def scrub(
    text: str,
    document_flagged_sensitive: bool = False,
    entity_allowlist: Optional[List[str]] = None,
) -> SensitivityResult:
    """Apply regex patterns and (if spaCy is available) NER entity
    redaction.

    Returns a :class:`SensitivityResult` with the redacted text and a list
    of pattern/entity names that fired (for the audit log).

    If ``document_flagged_sensitive`` is True the caller has marked the
    whole document as sensitive — return an empty redacted_text and skip
    downstream processing.
    """
    if document_flagged_sensitive:
        return SensitivityResult(redacted_text="", matches=["FLAGGED_SENSITIVE"], skip_document=True)
    if not text:
        return SensitivityResult(redacted_text="", matches=[])

    matches: List[str] = []
    try:
        out = _apply_regex_patterns(text, matches)
    except Exception as e:  # noqa: BLE001
        logger.warning("Regex redaction pass failed: %s", e)
        out = text
    try:
        out = _apply_entity_redaction(out, entity_allowlist or [], matches)
    except Exception as e:  # noqa: BLE001
        logger.warning("Entity redaction pass failed: %s", e)
    return SensitivityResult(redacted_text=out, matches=matches)


def scrub_batch(texts: List[str]) -> List[SensitivityResult]:
    return [scrub(t) for t in texts]

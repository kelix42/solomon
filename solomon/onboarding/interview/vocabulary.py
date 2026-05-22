"""Vocabulary capture — owner phrasing → vocabulary rows.

Two passes (Drive's `solomon-vocabulary-capture`):

  Pass 1 — spaCy NP/VP chunks. Deterministic, fast, free. Optional: if
    spaCy or the en_core_web_sm model isn't installed, we log a warning
    and skip this pass (the LLM idiom pass still runs).

  Pass 2 — Sonnet idiom / metaphor / stock-expression extraction
    (~200 tokens out).

Normalisation (canonical, per the column comment in
solomon/storage/schema.sql vocabulary table):
  - lowercase
  - strip surrounding punctuation
  - collapse internal whitespace
  - strip leading/trailing articles ("the", "a", "an")
  - hyphens preserved
  - NO stemming

Citation: docs/REPORT-INTERVIEW.md §1.1.3.
Drive source: skills/interview/solomon-vocabulary-capture/SKILL.md.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from ...reasoning.llm import get_client
from ...storage.pool import cursor, execute, get_conn

logger = logging.getLogger("solomon.onboarding.vocabulary")

_ARTICLES = ("the ", "a ", "an ")
_TRAILING_ARTICLES = (" the", " a", " an")

# Lazy spaCy load. None until first call; False once we know it's unavailable.
_SPACY_NLP: Any = None
_SPACY_TRIED: bool = False


def _get_spacy() -> Optional[Any]:
    """Return the loaded spaCy pipeline, or None if unavailable."""
    global _SPACY_NLP, _SPACY_TRIED
    if _SPACY_TRIED:
        return _SPACY_NLP
    _SPACY_TRIED = True
    try:
        import spacy  # type: ignore
    except ImportError:
        logger.info(
            "spaCy not installed; vocabulary Pass 1 (NP/VP chunks) will skip. "
            "Run `solomon onboard postinstall` to enable."
        )
        return None
    try:
        _SPACY_NLP = spacy.load("en_core_web_sm")
    except Exception:  # noqa: BLE001
        logger.info(
            "spaCy en_core_web_sm model not installed; vocabulary Pass 1 "
            "will skip. Run `solomon onboard postinstall` to install."
        )
        _SPACY_NLP = None
    return _SPACY_NLP


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalise(phrase: str) -> str:
    """Apply the canonical normalisation rule. Returns the key under which
    the phrase is stored in `vocabulary.normalised`.
    """
    if not phrase:
        return ""
    s = phrase.lower().strip()
    # Strip surrounding punctuation, preserve hyphens.
    s = re.sub(r"^[^\w\-]+|[^\w\-]+$", "", s)
    # Collapse internal whitespace.
    s = re.sub(r"\s+", " ", s)
    # Strip leading article.
    for art in _ARTICLES:
        if s.startswith(art):
            s = s[len(art):]
            break
    # Strip trailing article (rare).
    for art in _TRAILING_ARTICLES:
        if s.endswith(art):
            s = s[: -len(art)]
            break
    return s.strip()


# ---------------------------------------------------------------------------
# Pass 1: spaCy NP/VP chunks
# ---------------------------------------------------------------------------

def _pass1_spacy(text: str) -> List[Tuple[str, str]]:
    """Return [(phrase, kind)] where kind ∈ {noun_phrase, verb_phrase}."""
    nlp = _get_spacy()
    if nlp is None or not text:
        return []
    out: List[Tuple[str, str]] = []
    try:
        doc = nlp(text)
        for nc in getattr(doc, "noun_chunks", []):
            ph = nc.text.strip()
            if ph and len(ph) > 1:
                out.append((ph, "noun_phrase"))
        # Cheap VP detection: a verb plus its immediate dependents.
        for tok in doc:
            if tok.pos_ == "VERB":
                span = doc[tok.left_edge.i : tok.right_edge.i + 1]
                ph = span.text.strip()
                if 2 <= len(ph.split()) <= 6:
                    out.append((ph, "verb_phrase"))
    except Exception as e:  # noqa: BLE001
        logger.warning("spaCy Pass 1 failed: %s", e)
    return out


# ---------------------------------------------------------------------------
# Pass 2: LLM idiom / metaphor / stock expression
# ---------------------------------------------------------------------------

_IDIOM_SYSTEM = (
    "Extract idioms, metaphors, or stock expressions from the owner's text. "
    "These are turns of phrase that carry meaning beyond their literal words "
    "('nickel and dime', 'on the back of', 'kick the can'). Do NOT extract "
    "ordinary descriptions, proper nouns, or technical terms unless they "
    "function as a metaphor in context. Return JSON: "
    "{\"phrases\": [{\"phrase\": str, \"kind\": \"idiom\"|\"metaphor\"|\"stock_expression\"}]}. "
    "Return at most 5 phrases. If none, return {\"phrases\": []}."
)


def _pass2_llm(text: str) -> List[Tuple[str, str]]:
    if not text or not text.strip():
        return []
    client = get_client()
    try:
        resp = client.call(
            tier="fast",
            system=_IDIOM_SYSTEM,
            user=text,
            json_mode=True,
            max_tokens=200,
            temperature=0.1,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Idiom LLM call failed: %s", e)
        return []
    parsed = client.parse_json(resp.text) or {}
    phrases = parsed.get("phrases") or []
    if not isinstance(phrases, list):
        return []
    out: List[Tuple[str, str]] = []
    for p in phrases:
        if not isinstance(p, dict):
            continue
        ph = (p.get("phrase") or "").strip()
        kind = (p.get("kind") or "idiom").strip().lower()
        if kind not in ("idiom", "metaphor", "stock_expression"):
            kind = "idiom"
        if ph:
            out.append((ph, kind))
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def capture(
    owner_text: str,
    tenant_id: str,
    source_item_id: Optional[str] = None,
) -> int:
    """Run both passes over the owner's text. Upsert into vocabulary.

    Returns the number of *new* phrase keys inserted (not including freq bumps
    on existing phrases).
    """
    if not owner_text or not owner_text.strip():
        return 0

    phrases: List[Tuple[str, str]] = []
    phrases.extend(_pass1_spacy(owner_text))
    phrases.extend(_pass2_llm(owner_text))

    if not phrases:
        return 0

    new_inserted = 0
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                for ph, kind in phrases:
                    normalised = normalise(ph)
                    if not normalised or len(normalised) < 2:
                        continue
                    # Try update first; if no row exists, insert.
                    execute(
                        cur,
                        "UPDATE vocabulary SET frequency = frequency + 1, "
                        "last_seen = datetime('now') "
                        "WHERE tenant_id=? AND normalised=?",
                        (tenant_id, normalised),
                    )
                    if getattr(cur, "rowcount", 0) and cur.rowcount > 0:
                        continue
                    execute(
                        cur,
                        "INSERT INTO vocabulary (tenant_id, phrase, normalised, "
                        "kind, frequency, example_source_id) "
                        "VALUES (?, ?, ?, ?, 1, ?)",
                        (tenant_id, ph, normalised, kind, source_item_id),
                    )
                    new_inserted += 1
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.error("vocabulary upsert failed: %s", e)
        return 0
    return new_inserted

"""Interview engine — per-turn probe selection.

Pure SQL + YAML, no LLM. Implements the 8-step process documented in the
Drive's `solomon-interview-engine/SKILL.md`:

  1. Read clarification_queue WHERE session_id=? AND status='pending'.
     Pending clarifications jump the queue — asked verbatim.
  2. Else detect keywords in the owner's last answer.
  3. Open probe_library/<domain>.yaml for the active domain.
  4. Pick highest-priority unused probe (lowest priority number wins)
     for a matched keyword that hasn't hit coverage saturation.
  5. Render template with verbatim {phrase} substitution.
  6. Ask one question. Never stack.
  7. On dry keyword, fall back to a related keyword in the same domain,
     then to a generic forward prompt from _generic.yaml.
  8. After asking, increment coverage.probes_asked and set
     coverage.library_version_seen.

Citation: docs/REPORT-INTERVIEW.md §1.1.1, §4.3.
Drive source: skills/interview/solomon-interview-engine/SKILL.md.
"""

from __future__ import annotations

import logging
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ...storage.pool import cursor, execute, get_conn

logger = logging.getLogger("solomon.onboarding.engine")

PROBE_LIBRARY_DIR = Path(__file__).resolve().parent.parent / "probe_library"

# Cheap stop-list for the phrase extractor. Not exhaustive; the goal is to
# leave the owner's meaningful chunk intact (a noun or verb phrase).
_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "so", "if", "of", "to", "in",
    "on", "for", "at", "by", "with", "from", "as", "is", "was", "were",
    "are", "be", "been", "being", "this", "that", "these", "those", "it",
    "we", "i", "you", "he", "she", "they", "them", "our", "my", "your",
    "their", "his", "her", "its", "do", "does", "did", "have", "has",
    "had", "will", "would", "could", "should", "can", "may", "might",
    "just", "really", "very", "kind", "sort", "like", "yeah", "okay", "ok",
}

# in-process YAML cache
_LIBRARY_CACHE: Dict[str, Dict[str, Any]] = {}


def load_library(domain: str) -> Dict[str, Any]:
    """Read and cache the probe library YAML for a domain.

    Falls back to _generic.yaml when the requested domain file is missing.
    Always returns a dict, never None.
    """
    key = domain
    if key in _LIBRARY_CACHE:
        return _LIBRARY_CACHE[key]
    path = PROBE_LIBRARY_DIR / f"{domain}.yaml"
    if not path.exists():
        logger.warning("Probe library for domain '%s' not found at %s; using _generic.", domain, path)
        path = PROBE_LIBRARY_DIR / "_generic.yaml"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to load probe library %s: %s", path, e)
        data = {}
    _LIBRARY_CACHE[key] = data
    return data


def _load_generic() -> Dict[str, Any]:
    return load_library("_generic")


# ---------------------------------------------------------------------------
# Pending clarification (priority 1)
# ---------------------------------------------------------------------------

def _next_pending_clarification(session_id: str) -> Optional[Tuple[int, str]]:
    """Return (clarification_id, suggested_probe_text) or None.

    The Drive uses a separate `suggested_probe` column; our schema stores
    the suggestion inside `reason` (free text) and a stable rendering is
    enough for v1. Returns the oldest pending row for this session.
    """
    sql = (
        "SELECT clarification_id, reason FROM clarification_queue "
        "WHERE session_id=? AND status='pending' "
        "ORDER BY clarification_id ASC LIMIT 1"
    )
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(cur, sql, (session_id,))
                row = cur.fetchone()
        if not row:
            return None
        cid = row[0] if not hasattr(row, "keys") else row["clarification_id"]
        reason = row[1] if not hasattr(row, "keys") else row["reason"]
        return int(cid), str(reason or "")
    except Exception as e:  # noqa: BLE001
        logger.warning("clarification_queue lookup failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Keyword detection + {phrase} extraction
# ---------------------------------------------------------------------------

def _detect_keywords(text: str, library_keywords: Dict[str, Any]) -> List[str]:
    """Return the library keys that appear (case-insensitive substring) in text."""
    if not text or not library_keywords:
        return []
    haystack = text.lower()
    hits: List[str] = []
    for key in library_keywords.keys():
        k = str(key).lower().strip()
        if not k:
            continue
        if k in haystack:
            hits.append(str(key))
    return hits


def _extract_phrase(text: str) -> str:
    """Pull a verbatim {phrase} slot from the owner's last answer.

    Heuristic: the last short, non-trivial chunk of the answer (3–7 content
    words) tends to be the most echo-able. If that fails, fall back to the
    first chunk. The point is NOT to be clever; the point is to return
    something the owner literally said so the rule-1 verbatim contract holds.
    """
    if not text:
        return ""
    # Split on sentence terminators and clause breaks.
    chunks = [c.strip() for c in re.split(r"[.!?\n;]", text) if c.strip()]
    if not chunks:
        return text.strip()[:120]
    # Prefer the LAST clause (most recent thought) unless it's trivial.
    candidates = list(reversed(chunks))
    for chunk in candidates:
        words = chunk.split()
        content = [w for w in words if w.lower().strip(",:'\"") not in _STOPWORDS]
        if 2 <= len(content) <= 12:
            return chunk
    # Nothing in the sweet spot — return the shortest non-empty clause.
    return min(chunks, key=len)


# ---------------------------------------------------------------------------
# Coverage check (used to skip saturated keywords)
# ---------------------------------------------------------------------------

def _saturated_keywords(session_id: str, domain: str) -> List[str]:
    """Return sub_topic names that are already saturated (gap_score < 0.4
    AND probes_asked >= 5). The engine won't pick a saturated keyword.
    """
    sql = (
        "SELECT sub_topic FROM coverage "
        "WHERE session_id=? AND domain=? "
        "AND gap_score < 0.4 AND probes_asked >= 5"
    )
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(cur, sql, (session_id, domain))
                rows = cur.fetchall()
        return [r[0] if not hasattr(r, "keys") else r["sub_topic"] for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.warning("coverage saturation lookup failed: %s", e)
        return []


def _record_probe_asked(session_id: str, domain: str, sub_topic: str, library_version: str) -> None:
    """Upsert a coverage row and increment probes_asked + turns_since_last_capture."""
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "INSERT INTO coverage (tenant_id, session_id, domain, sub_topic, "
                    "probes_asked, library_version_seen) "
                    "SELECT tenant_id, ?, ?, ?, 1, ? FROM sessions WHERE session_id=? "
                    "ON CONFLICT(tenant_id, session_id, domain, sub_topic) DO UPDATE SET "
                    "probes_asked = probes_asked + 1, "
                    "turns_since_last_capture = turns_since_last_capture + 1, "
                    "library_version_seen = excluded.library_version_seen, "
                    "last_updated = datetime('now')",
                    (session_id, domain, sub_topic, library_version, session_id),
                )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("coverage upsert failed: %s", e)


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

def _render_template(template: str, phrase: str) -> str:
    """Substitute {phrase} verbatim. Strip stacked echoes if phrase is empty."""
    if "{phrase}" not in template:
        return template
    if not phrase:
        # Strip a leading "{phrase}. " pattern so the question still reads.
        cleaned = re.sub(r"^\{phrase\}[.\s,:-]+", "", template).strip()
        return cleaned or template.replace("{phrase}", "").strip()
    return template.replace("{phrase}", phrase.strip().rstrip(".!?,"))


def _best_template(keyword_block: Any) -> Optional[str]:
    """Pick the lowest-priority-number template from a keyword's block.

    The block is a list of {priority, template} dicts in our YAML format.
    """
    if not keyword_block:
        return None
    if isinstance(keyword_block, str):
        return keyword_block
    if isinstance(keyword_block, list):
        sortable = []
        for entry in keyword_block:
            if isinstance(entry, dict) and "template" in entry:
                sortable.append((int(entry.get("priority", 999)), str(entry["template"])))
            elif isinstance(entry, str):
                sortable.append((999, entry))
        if not sortable:
            return None
        sortable.sort(key=lambda x: x[0])
        return sortable[0][1]
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def select_next_probe(
    session_id: str,
    domain: str,
    last_answer_text: str = "",
) -> str:
    """Return the next question to ask the owner.

    Thin wrapper over :func:`select_next_probe_with_meta` that drops the
    metadata. Kept for back-compat with existing tests; new callers
    should use the meta variant so they can tag the resulting capture
    with the right ``field:<id>`` keyword.
    """
    probe, _ = select_next_probe_with_meta(session_id, domain, last_answer_text)
    return probe


def select_next_probe_with_meta(
    session_id: str,
    domain: str,
    last_answer_text: str = "",
) -> Tuple[str, Optional[str]]:
    """Return ``(probe_text, required_field_id_or_None)`` for the next turn.

    No LLM call. Pure SQL + YAML. Order of resolution:
      1. Pending clarification (clarification_queue.status='pending') →
         render the suggested probe verbatim. Returns ``(probe, None)``.
      2. Keyword match against the owner's last answer → render the
         highest-priority template with {phrase} substitution. Returns
         ``(probe, None)``.
      3. Unfilled required field → ask its prompt verbatim. Prefer
         declaration order so the most-basic question lands first; this
         is what makes the cold open of every session start with the
         right question instead of a random domain fallback. Returns
         ``(prompt, field_id)`` so the caller can tag the resulting
         capture with ``field:<id>``.
      4. Domain fallback (random pick from library's `fallbacks:`).
         Returns ``(probe, None)``.
      5. Generic fallback (_generic.yaml::fallbacks). Returns
         ``(probe, None)``.
    """
    # 1. Pending clarification
    pending = _next_pending_clarification(session_id)
    if pending:
        _, suggestion = pending
        if suggestion and suggestion.strip():
            return suggestion.strip(), None

    library = load_library(domain)
    version = str(library.get("version") or "0.0.0")
    keyword_map: Dict[str, Any] = library.get("keywords") or {}
    saturated = set(_saturated_keywords(session_id, domain))

    # 2. Keyword match
    matched = [k for k in _detect_keywords(last_answer_text, keyword_map) if k not in saturated]
    if matched:
        # Domain priority is encoded per-keyword in our schema; pick the
        # first keyword whose lowest-priority-number template renders.
        for keyword in matched:
            template = _best_template(keyword_map.get(keyword))
            if template:
                phrase = _extract_phrase(last_answer_text)
                rendered = _render_template(template, phrase)
                _record_probe_asked(session_id, domain, keyword, version)
                return rendered, None

    # 3. Unfilled required field (declaration order)
    #
    # If the owner's last answer didn't produce a keyword match — which
    # always happens on turn 1 because last_answer is empty — fall back
    # to the next unanswered required field instead of a random domain
    # fallback. The first required field in each library is intended to
    # be the most basic question, so this guarantees a sensible cold open.
    required = library.get("required_fields") or []
    rf_ids: List[str] = [
        str(f["id"]) for f in required
        if isinstance(f, dict) and f.get("id")
    ]
    if rf_ids:
        # Lazy import — avoids a circular import at module load.
        from . import coverage as _coverage
        gaps = _coverage.required_field_gaps(session_id, rf_ids)
        if gaps:
            rf_lookup = {f["id"]: f for f in required if isinstance(f, dict) and f.get("id")}
            field = rf_lookup.get(gaps[0]) or {}
            prompt = field.get("prompt")
            if prompt:
                _record_probe_asked(session_id, domain, f"_required:{gaps[0]}", version)
                return str(prompt), gaps[0]

    # 4. Domain fallback
    fallbacks: List[str] = list(library.get("fallbacks") or [])
    if fallbacks:
        choice = random.choice(fallbacks)
        _record_probe_asked(session_id, domain, "_fallback", version)
        return choice, None

    # 5. Generic fallback
    generic = _load_generic()
    g_fallbacks: List[str] = list(generic.get("fallbacks") or [])
    if g_fallbacks:
        _record_probe_asked(session_id, domain, "_generic", version)
        return random.choice(g_fallbacks), None

    # Last-resort safety probe.
    return "Tell me about the last time that came up in your business.", None

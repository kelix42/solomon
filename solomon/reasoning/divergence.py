"""Part 9: surprise score between System 1 and System 2 answers.

Drive source: ``orchestrator/pipeline/stage_system2.py`` line 42 — the
``0.6 * jaccard + 0.4 * (1 - length_ratio)`` formula. The length-ratio
term distinguishes paraphrases of different lengths from true
disagreements; pure Jaccard treats "approve" vs "approve immediately"
as having identical token overlap on the short side but missing tokens
on the long side.

Report §3 "Token-Jaccard divergence formula" picks this hybrid as the
v1 choice over pure-Jaccard or embeddings (embeddings remain a future
upgrade path once the embedder is wired up).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("solomon.reasoning")

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set:
    """Lowercase, split on non-alphanumerics, drop empties."""
    if not text:
        return set()
    return set(_TOKEN_RE.findall(text.lower()))


def _jaccard_distance(t1: set, t2: set) -> float:
    """1 - |intersection| / |union|. Caller must ensure both sets non-empty."""
    inter = t1 & t2
    union = t1 | t2
    if not union:
        return 0.0
    return 1.0 - (len(inter) / len(union))


def _length_ratio(s1: str, s2: str) -> float:
    """min(|s1|, |s2|) / max(|s1|, |s2|). 1.0 = same length, 0.0 = one empty."""
    l1, l2 = len(s1 or ""), len(s2 or "")
    if max(l1, l2) == 0:
        return 1.0
    return min(l1, l2) / max(l1, l2)


def divergence_score(s1_text: str, s2_text: str) -> float:
    """Hybrid token-Jaccard + length-ratio divergence (Drive formula).

    ``0.6 * jaccard_distance + 0.4 * (1 - length_ratio)``.

    Returns:
        0.0  → identical token sets and identical lengths
        1.0  → no token overlap and one side empty (max divergence)
        0.5  → at least one side is empty / no signal
    """
    try:
        if not s1_text or not s2_text:
            return 0.5

        t1 = _tokenize(s1_text)
        t2 = _tokenize(s2_text)

        if not t1 or not t2:
            return 0.5

        jaccard = _jaccard_distance(t1, t2)
        length_ratio = _length_ratio(s1_text, s2_text)

        score = 0.6 * jaccard + 0.4 * (1.0 - length_ratio)
        # Clamp for safety.
        return max(0.0, min(1.0, score))
    except Exception as e:  # noqa: BLE001
        logger.warning("divergence_score failed: %s", e)
        return 0.5

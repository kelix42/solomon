"""Part 9: surprise score between System 1 and System 2 answers.

This is the cheap, first-cut divergence metric: token-level Jaccard distance.
Big number ⇒ S1 and S2 are saying very different things ⇒ this decision is
surprising ⇒ wake the audit gate and tag the event for nightly replay.

TODO: upgrade to an embedding-based cosine distance once we wire up an
embedding model — Jaccard misses paraphrase-level disagreement ("approve" vs
"green-light" look identical to humans but score as fully divergent here).
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


def divergence_score(s1_text: str, s2_text: str) -> float:
    """Jaccard distance between the two answers' token sets.

    Returns:
        0.0  → identical token sets
        1.0  → no overlap
        0.5  → at least one side is empty (no signal)
    """
    try:
        if not s1_text or not s2_text:
            return 0.5

        t1 = _tokenize(s1_text)
        t2 = _tokenize(s2_text)

        if not t1 or not t2:
            return 0.5

        intersection = t1 & t2
        union = t1 | t2

        if not intersection:
            return 1.0
        if not union:
            # Defensive — should be impossible given the empty checks above.
            return 0.5

        return 1.0 - (len(intersection) / len(union))
    except Exception as e:  # noqa: BLE001
        logger.warning("divergence_score failed: %s", e)
        return 0.5

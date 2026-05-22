"""Thin LLM client wrapper for the corpus passes.

REPORT-CORPUS.md §4.5 Phase B: the Drive's Anthropic-direct calls become
``solomon.reasoning.llm.get_client().call(tier='deep', ...)``. This module
centralises that swap and provides JSON-envelope parsing helpers shared
by ``llm_passes`` and ``rules``.

Single public function:

  ``call(system, user, *, max_tokens=4096, temperature=0.2, tier='deep') -> str``

Returns the raw assistant text. Callers parse JSON / strip fences themselves
because each pass has its own envelope shape.

The function returns the empty string on LLM failure (the underlying
client logs the warning) so callers can decide whether to mark the file
``partial`` or to skip a step entirely.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..reasoning.llm import get_client

logger = logging.getLogger("solomon.corpus.llm")


def call(
    *,
    system: str,
    user: str,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    tier: str = "deep",
    json_mode: bool = False,
) -> str:
    """Make one LLM call. Returns the assistant text (possibly empty)."""
    client = get_client()
    resp = client.call(
        tier=tier,
        system=system,
        user=user,
        json_mode=json_mode,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return resp.text or ""

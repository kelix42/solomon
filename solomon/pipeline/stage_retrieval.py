"""Stage 5 — Retrieval (working memory + 5-lane long-term).

Drive source: ``orchestrator/pipeline/stage_retrieval.py``. Report §3 line 44 —
"5-lane: Pinecone semantic + recency + entity + pressure + foundation.
Currently stubbed". We wrap the existing
``solomon.memory.working.WorkingMemory`` + ``solomon.memory.retrieval.MultiLaneRetrieval``
modules; they handle the 5 lanes internally.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..memory.retrieval import MultiLaneRetrieval
from ..memory.working import WorkingMemory
from ._helpers import update_event
from .stage_salience import _StubAdapter, _row_to_raw_event

logger = logging.getLogger("solomon.pipeline.retrieval")


def run(event_id: str, event_row: dict, *, adapter: Optional[Any] = None) -> dict:
    """Fetch hot context + long-term retrieval. Writes retrieval_context JSON."""
    adapter = adapter or _StubAdapter()
    raw_event = _row_to_raw_event(event_row)
    cls = event_row.get("classification") or {}
    scope = cls.get("scope") if isinstance(cls, dict) else None
    domain = cls.get("domain") if isinstance(cls, dict) else None

    context_blob: dict = {
        "working_memory_used": False,
        "lanes_used": [],
        "heuristic_ids": [],
        "foundation_files": [],
    }

    try:
        wm = WorkingMemory(adapter)
        hot = wm.fetch(scope=scope, raw_event=raw_event)
        thin = hot.is_thin() if hasattr(hot, "is_thin") else True
        context_blob["working_memory_used"] = not thin

        if thin:
            ret = MultiLaneRetrieval(adapter)
            long_ctx = ret.retrieve(raw_event, scope=scope, domain=domain)
            context_blob["lanes_used"] = list(getattr(long_ctx, "lanes", []) or [])
            context_blob["heuristic_ids"] = list(getattr(long_ctx, "heuristic_ids", []) or [])
            context_blob["foundation_files"] = list(getattr(long_ctx, "foundation_files", []) or [])
    except Exception as e:  # noqa: BLE001
        logger.warning("stage_retrieval failed (continuing): %s", e)
        context_blob["error"] = f"{type(e).__name__}: {e}"

    update_event(event_id, retrieval_context=context_blob)
    event_row["retrieval_context"] = context_blob
    return event_row

"""Solomon corpus pipeline — Karpathy LLM-Wiki pattern over local storage.

See `docs/REPORT-CORPUS.md` §3 ("Best-of-both") and §4 ("Integration plan").

Drive's four Pinecone namespaces become a ``source_table`` column on the
shared ``embeddings`` table. The retrieval re-ranker still applies the
0.40 / 0.30 / 0.20 / 0.10 weight vector across the four logical
namespaces — only the storage backend changes.

Public re-exports:
  - ``NAMESPACE_WEIGHTS``       — Lane-1 namespace weight vector
  - ``ingest_file(path)``       — orchestrator entry point
  - ``ingest_directory(path)``  — recursive batch
"""

from __future__ import annotations

from typing import Dict

# Lane-1 default weights per REPORT-CORPUS.md §1.1 and §3.
# These are the per-namespace multipliers applied AFTER a pgvector /
# sqlite-vec nearest-neighbour query, before the lane-merge step.
NAMESPACE_WEIGHTS: Dict[str, float] = {
    "corpus_wiki": 0.40,      # LLM-synthesized wiki pages — highest signal
    "captured_items": 0.30,   # owner's stated rules
    "corpus_raw": 0.20,       # grounding citations
    "decisions": 0.10,        # historical decision log
}

# Re-exports for convenience. Wrapped in try/except so the package
# imports cleanly even when individual submodules are still being
# brought up (the corpus pipeline is built one file at a time per
# BUILD-STATE.md).
try:
    from .ingest import ingest_file, ingest_directory  # noqa: E402,F401
except ImportError:  # pragma: no cover — only during incremental build
    pass

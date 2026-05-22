"""Corpus chunking — type-aware first, sliding-window fallback.

REPORT-CORPUS.md §4.5 Phase B: delegate to the existing type-aware
chunker in ``solomon.ingestion.chunker`` for documents that fit one of
its kinds (email_thread, transcript, contract, sop, internal_doc), and
fall back to the Drive's 800-token sliding window for prose that lacks
natural breaks.

Output is a ``Chunk`` dataclass with:
  - ``text``         — chunk content
  - ``seq``          — sequence index within the document
  - ``char_offsets`` — (start, end) into the source text
  - ``source_section`` — heading / speaker / message index, when known
  - ``metadata``     — passthrough of the ingestion chunker metadata

The ingestion chunker doesn't carry char offsets so we compute them
here by scanning the source text for each chunk's first 200 chars.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..ingestion.chunker import Chunk as IngestionChunk
from ..ingestion.chunker import chunk_document

logger = logging.getLogger("solomon.corpus.chunk")

# Sliding-window fallback parameters (Drive defaults — see schema_config.py).
CHARS_PER_TOKEN = 4
CHUNK_SIZE_TOKENS = 800
CHUNK_OVERLAP_TOKENS = 100
CHUNK_MIN_CHARS = 50  # below this we skip the chunk as signal-free

# Document types the ingestion chunker handles with natural breaks.
TYPE_AWARE_KINDS = {"email_thread", "transcript", "contract", "sop", "internal_doc"}


@dataclass
class Chunk:
    seq: int
    text: str
    char_offsets: Tuple[int, int]
    source_section: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def _source_section_for(meta: Dict[str, Any]) -> Optional[str]:
    """Pick a single best-fit section label from the chunker's metadata."""
    if not meta:
        return None
    for key in ("heading", "speaker", "section", "kind"):
        if key in meta and meta[key]:
            return str(meta[key])
    if "message_index" in meta:
        return f"message_{meta['message_index']}"
    return None


def _locate_offsets(haystack: str, needle: str, start_from: int = 0) -> Tuple[int, int]:
    """Find ``needle`` in ``haystack`` starting at ``start_from``.

    We probe with the first ~200 chars of the needle to survive the
    type-aware chunker's small reformatting (it prefixes transcript
    turns with "<speaker>: ", merges short turns with newlines, etc.).
    If we can't find a match at all we return (start_from, start_from +
    len(needle)) so the offsets are still well-formed.
    """
    if not needle:
        return (start_from, start_from)
    probe = needle[:200].strip()
    if not probe:
        return (start_from, start_from + len(needle))
    idx = haystack.find(probe, start_from)
    if idx < 0:
        # Try without the probe's prefix decoration (e.g. "Alice: ").
        first_line = probe.split("\n", 1)[0]
        if ":" in first_line[:60]:
            stripped = probe.split(":", 1)[1].strip() if ":" in probe else probe
            idx = haystack.find(stripped[:120], start_from)
        if idx < 0:
            idx = start_from
    return (idx, idx + len(needle))


def chunk(text: str, document_type: str = "other") -> List[Chunk]:
    """Top-level entry point.

    Routes by ``document_type``; for unknown / generic types we use the
    sliding-window fallback so we always return something embeddable.
    """
    if not text or not text.strip():
        return []
    if document_type in TYPE_AWARE_KINDS:
        type_aware = chunk_document(text, document_type)
        if type_aware:
            return _adapt_type_aware(text, type_aware)
    return sliding_window(text)


def _adapt_type_aware(source: str, chunks: List[IngestionChunk]) -> List[Chunk]:
    out: List[Chunk] = []
    cursor = 0
    for ic in chunks:
        start, end = _locate_offsets(source, ic.text, cursor)
        cursor = max(cursor, start)
        out.append(
            Chunk(
                seq=ic.seq,
                text=ic.text,
                char_offsets=(start, end),
                source_section=_source_section_for(ic.metadata),
                metadata=dict(ic.metadata or {}),
            )
        )
    return out


def sliding_window(
    text: str,
    chunk_size_tokens: int = CHUNK_SIZE_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> List[Chunk]:
    """Drive-style char-approximated token window.

    1 token ~= 4 chars for English text. Step = chunk_chars - overlap_chars
    so each chunk shares ``overlap_chars`` with the previous one. Chunks
    shorter than CHUNK_MIN_CHARS are skipped as signal-free.
    """
    chunk_chars = chunk_size_tokens * CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * CHARS_PER_TOKEN
    step = max(chunk_chars - overlap_chars, 1)
    out: List[Chunk] = []
    pos = 0
    seq = 0
    n = len(text)
    while pos < n:
        end = min(pos + chunk_chars, n)
        piece = text[pos:end]
        stripped = piece.strip()
        if len(stripped) >= CHUNK_MIN_CHARS:
            # The stripped text may start later than `pos`; locate it.
            leading = len(piece) - len(piece.lstrip())
            start_off = pos + leading
            end_off = start_off + len(stripped)
            out.append(
                Chunk(
                    seq=seq,
                    text=stripped,
                    char_offsets=(start_off, end_off),
                    source_section=None,
                    metadata={"kind": "sliding_window"},
                )
            )
            seq += 1
        pos += step
    return out

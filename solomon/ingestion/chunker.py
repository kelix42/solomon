"""Type-specific chunker. Splits a document into chunks the right way
for its type.

  - email_thread  -> one chunk per message in the thread
  - transcript    -> chunks by speaker turn, merging short turns
  - contract      -> chunks by section heading
  - sop           -> chunks by section heading
  - everything else -> generic paragraph chunking with a sane token cap

Each chunk gets a sequence number and a small bit of metadata (which
message, which speaker, which section). The chunker does NOT touch
content; sensitivity filtering happens elsewhere.

Chunk size target: ~500-1500 chars, with overlap=100 chars for generic
chunking so a fact split across a paragraph boundary still shows up in
retrieval on both sides.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger("solomon.ingestion.chunker")


@dataclass
class Chunk:
    seq: int
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)


def chunk_document(text: str, document_type: str) -> List[Chunk]:
    if not text or not text.strip():
        return []
    if document_type == "email_thread":
        return _chunk_email_thread(text)
    if document_type == "transcript":
        return _chunk_transcript(text)
    if document_type in ("contract", "sop", "internal_doc"):
        return _chunk_by_heading(text)
    return _chunk_generic(text)


# ---------------------------------------------------------------------------
# Email threads
# ---------------------------------------------------------------------------

EMAIL_BOUNDARY = re.compile(
    r"(?m)^(?:On\s.+wrote:|From:\s.+|>+\s|-----Original Message-----|^_{5,}$)",
)


def _chunk_email_thread(text: str) -> List[Chunk]:
    # Split on common quoted-reply boundaries. First piece is the most
    # recent message; subsequent pieces are older quoted messages.
    parts = EMAIL_BOUNDARY.split(text)
    parts = [p.strip() for p in parts if p and p.strip()]
    if not parts:
        return _chunk_generic(text)
    chunks: List[Chunk] = []
    for i, part in enumerate(parts):
        chunks.append(Chunk(seq=i, text=part, metadata={"message_index": i, "kind": "email_message"}))
    return chunks


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------

SPEAKER_TURN = re.compile(r"(?m)^\s*([A-Z][A-Za-z .'-]{0,40})\s*:\s")


def _chunk_transcript(text: str) -> List[Chunk]:
    matches = list(SPEAKER_TURN.finditer(text))
    if len(matches) < 2:
        return _chunk_generic(text)
    chunks: List[Chunk] = []
    for i, m in enumerate(matches):
        speaker = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        turn = text[start:end].strip()
        if not turn:
            continue
        chunks.append(
            Chunk(
                seq=i,
                text=f"{speaker}: {turn}",
                metadata={"speaker": speaker, "kind": "transcript_turn"},
            )
        )
    # Merge very short turns into the next one — back-and-forth "yes"
    # exchanges are noise.
    merged: List[Chunk] = []
    buffer: List[str] = []
    for ch in chunks:
        if len(ch.text) < 200:
            buffer.append(ch.text)
            continue
        if buffer:
            merged.append(Chunk(seq=len(merged), text="\n".join(buffer + [ch.text]), metadata=ch.metadata))
            buffer = []
        else:
            merged.append(Chunk(seq=len(merged), text=ch.text, metadata=ch.metadata))
    if buffer:
        merged.append(Chunk(seq=len(merged), text="\n".join(buffer), metadata={"kind": "transcript_turn"}))
    return merged or chunks


# ---------------------------------------------------------------------------
# Heading-based (contracts, SOPs, internal docs)
# ---------------------------------------------------------------------------

HEADING = re.compile(r"(?m)^(?:#{1,6}\s.+|[A-Z][A-Z0-9 \-]{4,80}|\d+(?:\.\d+)*\.\s.+)$")


def _chunk_by_heading(text: str) -> List[Chunk]:
    matches = list(HEADING.finditer(text))
    if len(matches) < 2:
        return _chunk_generic(text)
    chunks: List[Chunk] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section_text = text[start:end].strip()
        heading_line = m.group(0).strip()
        chunks.append(
            Chunk(
                seq=i,
                text=section_text,
                metadata={"heading": heading_line, "kind": "section"},
            )
        )
    return chunks


# ---------------------------------------------------------------------------
# Generic paragraph chunking
# ---------------------------------------------------------------------------

GENERIC_MAX = 1500
GENERIC_OVERLAP = 100


def _chunk_generic(text: str) -> List[Chunk]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[Chunk] = []
    buffer: List[str] = []
    buffer_len = 0
    seq = 0
    for para in paragraphs:
        if buffer_len + len(para) > GENERIC_MAX and buffer:
            chunk_text = "\n\n".join(buffer)
            chunks.append(Chunk(seq=seq, text=chunk_text, metadata={"kind": "paragraph"}))
            seq += 1
            # Overlap: keep the tail of the previous buffer as start of next.
            tail = chunk_text[-GENERIC_OVERLAP:] if GENERIC_OVERLAP and len(chunk_text) > GENERIC_OVERLAP else ""
            buffer = [tail] if tail else []
            buffer_len = len(tail)
        buffer.append(para)
        buffer_len += len(para)
    if buffer:
        chunks.append(Chunk(seq=seq, text="\n\n".join(buffer), metadata={"kind": "paragraph"}))
    return chunks

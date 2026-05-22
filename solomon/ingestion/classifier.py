"""Document classifier. One LLM call per document.

Detects document type, time period, participants, domain, and a salience
estimate. Output drives:
  - which chunker to use (email vs transcript vs contract)
  - which decisions table fields to fill
  - whether to do the deep extraction passes or just embed-and-store

If the LLM is unavailable or the call fails, the classifier returns
ClassificationResult with sensible defaults so the pipeline can continue.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from ..reasoning.llm import get_client

logger = logging.getLogger("solomon.ingestion.classifier")


DOCUMENT_TYPES = (
    "email_thread", "proposal", "transcript", "contract", "sop",
    "feedback", "internal_doc", "text_exchange", "note", "spreadsheet", "other",
)


@dataclass
class DocumentClassification:
    document_type: str = "other"
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    participants: List[str] = field(default_factory=list)
    domain: Optional[str] = None
    salience_estimate: float = 0.5


def classify_document(text: str, filename: str = "") -> DocumentClassification:
    """Best-effort classification. Always returns a valid result."""
    if not text or not text.strip():
        return DocumentClassification()

    client = get_client()
    if not client.configured:
        # Fall back to filename-based guess.
        return _guess_from_filename(filename)

    # Truncate the text — first 4k chars is plenty for classification.
    excerpt = text[:4000]
    sample_types = ", ".join(DOCUMENT_TYPES)
    prompt = (
        f"Document excerpt:\n---\n{excerpt}\n---\n\n"
        f"Filename: {filename or '(unknown)'}\n\n"
        f"Classify this document. Return JSON with keys:\n"
        f"  document_type: one of [{sample_types}]\n"
        f"  period_start: ISO date if you can tell when it was written, else null\n"
        f"  period_end: ISO date if there's a clear range, else null\n"
        f"  participants: list of names/emails involved (max 10)\n"
        f"  domain: one phrase describing the business area (pricing, hiring, vendor, etc.)\n"
        f"  salience_estimate: float 0-1 of how decision-rich this looks\n"
    )

    resp = client.call(
        tier="fast",
        system="You classify business documents for ingestion. Return strict JSON.",
        user=prompt,
        json_mode=True,
        max_tokens=512,
        temperature=0.1,
    )
    parsed = client.parse_json(resp.text) or {}
    return _coerce(parsed)


def _coerce(d: dict) -> DocumentClassification:
    dt = d.get("document_type", "other")
    if dt not in DOCUMENT_TYPES:
        dt = "other"
    out = DocumentClassification(
        document_type=dt,
        domain=d.get("domain") or None,
        participants=[str(p) for p in (d.get("participants") or [])][:10],
        salience_estimate=_clamp(float(d.get("salience_estimate", 0.5) or 0.5)),
    )
    for field_name in ("period_start", "period_end"):
        val = d.get(field_name)
        if val:
            try:
                # Handle plain dates and full ISO timestamps.
                out.__setattr__(field_name, datetime.fromisoformat(str(val).replace("Z", "+00:00")))
            except Exception:  # noqa: BLE001
                pass
    return out


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _guess_from_filename(filename: str) -> DocumentClassification:
    if not filename:
        return DocumentClassification()
    f = filename.lower()
    if f.endswith((".eml", ".mbox")) or "email" in f:
        return DocumentClassification(document_type="email_thread")
    if "transcript" in f or "meeting" in f or f.endswith((".vtt", ".srt")):
        return DocumentClassification(document_type="transcript")
    if "contract" in f or "agreement" in f:
        return DocumentClassification(document_type="contract")
    if "sop" in f or "procedure" in f or "policy" in f:
        return DocumentClassification(document_type="sop")
    if "proposal" in f or "quote" in f:
        return DocumentClassification(document_type="proposal")
    return DocumentClassification()

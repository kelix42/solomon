"""Decision extractor — Part 26 Stage 4.

For each chunk that looks like it carries a business decision, call the deep
LLM to pull out structured fields (situation, options, decision, reasoning,
outcome, decision-maker, timestamp) and persist them into the `decisions`
table as historical entries.

This is the bridge between raw documents and the decision log that the rest
of Solomon reasons over. We're deliberately conservative: a cheap keyword
pre-filter skips chunks that obviously don't contain a decision, and the
LLM's self-reported confidence has to clear a floor before we store.

All errors are swallowed with a logged warning — ingestion must never crash
the pipeline. The worst case is a chunk we silently skip.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..reasoning.llm import get_client
from ..storage.pool import get_pool

logger = logging.getLogger("solomon.ingestion.extractor")


# Cheap pre-filter. If none of these tokens appear in the chunk we don't
# bother spending a deep-tier LLM call on it. Tuned for recall over precision;
# the LLM is the real filter.
_DECISION_KEYWORDS = (
    "decided", "chose", "agreed", "will", "going to", "plan to",
    "proposed", "accepted", "rejected", "quote", "offer",
)


@dataclass
class ExtractedDecision:
    """Structured decision pulled from a single document chunk."""

    situation: str
    options_considered: List[str] = field(default_factory=list)
    decision: str = ""
    reasoning: str = ""
    outcome: Optional[str] = None
    decision_maker: Optional[str] = None
    timestamp: Optional[datetime] = None
    confidence: float = 0.0
    # Free-form annotations the caller can use without polluting the dataclass
    # contract (e.g. "decision_maker is not the tenant owner").
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _has_decision_signal(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(kw in lowered for kw in _DECISION_KEYWORDS)


def _parse_timestamp(val: Any) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _clamp_unit(x: Any) -> float:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


def extract_from_chunk(
    chunk_text: str,
    document_metadata: dict,
) -> Optional[ExtractedDecision]:
    """Extract a structured decision from a single chunk, or None.

    Pipeline:
      1. Pre-filter on decision-like keywords (free).
      2. Deep-tier LLM call with JSON mode.
      3. Confidence floor of 0.3 — below that we discard.

    document_metadata is opaque dict passed through; we read 'tenant_id'
    if present so we can flag third-party decisions in .metadata, but
    nothing here requires any specific shape.
    """
    if not chunk_text or not chunk_text.strip():
        return None
    if not _has_decision_signal(chunk_text):
        return None

    client = get_client()
    if not client.configured:
        logger.debug("Extractor: LLM not configured, skipping chunk.")
        return None

    # Cap chunk size we send to the LLM. Chunks should already be bounded
    # by the chunker but defensive truncation is cheap.
    excerpt = chunk_text[:6000]

    doc_hints = []
    if document_metadata:
        if document_metadata.get("document_type"):
            doc_hints.append(f"document_type={document_metadata['document_type']}")
        if document_metadata.get("domain"):
            doc_hints.append(f"domain={document_metadata['domain']}")
        if document_metadata.get("period_start"):
            doc_hints.append(f"period_start={document_metadata['period_start']}")
        if document_metadata.get("participants"):
            doc_hints.append(
                f"participants={', '.join(str(p) for p in document_metadata['participants'][:8])}"
            )
    hint_line = ("Document context: " + "; ".join(doc_hints) + "\n\n") if doc_hints else ""

    system = (
        "You extract business decisions from document excerpts. "
        "Return strict JSON. Use null for any field the excerpt doesn't reveal. "
        "Do not invent details; if the chunk is just discussion with no concrete "
        "decision, return confidence below 0.3."
    )
    user = (
        f"{hint_line}"
        f"Excerpt:\n---\n{excerpt}\n---\n\n"
        f"Extract the decision (if any) as JSON with these keys:\n"
        f"  situation: string — what was being decided about\n"
        f"  options_considered: array of strings (each a brief option), [] if none stated\n"
        f"  decision: string — what was actually decided\n"
        f"  reasoning: string — why, as stated or strongly implied\n"
        f"  outcome: string or null — observable result if the excerpt shows it\n"
        f"  decision_maker: string or null — name/role of who decided\n"
        f"  timestamp: ISO 8601 string or null — when the decision was made\n"
        f"  confidence: float 0-1 — your confidence this is a real, well-formed decision\n"
    )

    try:
        resp = client.call(
            tier="deep",
            system=system,
            user=user,
            json_mode=True,
            max_tokens=1024,
            temperature=0.1,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Extractor LLM call failed: %s", e)
        return None

    parsed = client.parse_json(resp.text)
    if not parsed:
        logger.debug("Extractor: LLM returned unparseable JSON.")
        return None

    try:
        extracted = _coerce(parsed)
    except Exception as e:  # noqa: BLE001
        logger.warning("Extractor failed to coerce LLM output: %s", e)
        return None

    if extracted.confidence < 0.3:
        logger.debug(
            "Extractor: low confidence (%.2f), dropping.", extracted.confidence
        )
        return None

    # Tag third-party decisions so downstream heuristic mining can skip them.
    tenant_owner = (document_metadata or {}).get("owner_name") or ""
    tenant_id = (document_metadata or {}).get("tenant_id") or ""
    if extracted.decision_maker:
        dm = extracted.decision_maker.lower()
        if tenant_owner and tenant_owner.lower() not in dm and tenant_id.lower() not in dm:
            extracted.metadata["third_party_decision_maker"] = True

    return extracted


def _coerce(d: Dict[str, Any]) -> ExtractedDecision:
    options = d.get("options_considered") or []
    if not isinstance(options, list):
        options = []
    options = [str(o) for o in options if o is not None][:20]

    return ExtractedDecision(
        situation=str(d.get("situation") or "").strip(),
        options_considered=options,
        decision=str(d.get("decision") or "").strip(),
        reasoning=str(d.get("reasoning") or "").strip(),
        outcome=(str(d["outcome"]).strip() if d.get("outcome") else None),
        decision_maker=(str(d["decision_maker"]).strip() if d.get("decision_maker") else None),
        timestamp=_parse_timestamp(d.get("timestamp")),
        confidence=_clamp_unit(d.get("confidence", 0.0)),
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def store_extracted_decision(
    tenant_id: str,
    document_id: int,
    extracted: ExtractedDecision,
    scope: Optional[str],
    domain: Optional[str],
) -> Optional[int]:
    """Insert an extracted decision as a historical row. Returns decision_id.

    Maps the ExtractedDecision dataclass onto the columns that actually exist
    in the `decisions` table (see schema.sql):

      situation        -> system_2_answer (the narrative)
      decision         -> proposed_action and final_action (mirror; the action
                          was both proposed and taken in retrospect)
      reasoning        -> audit_reasoning
      outcome          -> owner_action (the observed result attributable to
                          the owner / world)
      options + maker + timestamp + ingestion provenance + confidence
                       -> stuffed into final_action as a tagged suffix so we
                          don't lose them; the schema has no native column.
                          (We additionally write the ingestion provenance
                          marker as required: 'ingested from document_id=N'.)
      classification_confidence -> extraction confidence
      historical       -> true

    Returns None on any DB error.
    """
    if not extracted:
        return None

    # Compose final_action: the literal decision text plus a structured
    # provenance/footnote block so we can recover the rich fields later
    # without schema changes.
    footnote: Dict[str, Any] = {
        "ingested_from_document_id": document_id,
        "options_considered": extracted.options_considered,
    }
    if extracted.decision_maker:
        footnote["decision_maker"] = extracted.decision_maker
    if extracted.timestamp:
        footnote["decision_timestamp"] = extracted.timestamp.isoformat()
    if extracted.metadata:
        footnote["metadata"] = extracted.metadata

    final_action = extracted.decision or extracted.situation or ""
    try:
        final_action = (
            f"{final_action}\n\n[solomon-ingest] {json.dumps(footnote, default=str)}"
        )
    except Exception:  # noqa: BLE001
        # If anything in footnote isn't JSON-serializable, fall back to a
        # human-readable note.
        final_action = (
            f"{final_action}\n\n[solomon-ingest] ingested from document_id={document_id}"
        )

    try:
        pool = get_pool()
    except Exception as e:  # noqa: BLE001
        logger.warning("Extractor: storage pool unavailable: %s", e)
        return None

    sql = """
        INSERT INTO decisions (
            tenant_id,
            scope,
            domain,
            classification_confidence,
            system_2_answer,
            proposed_action,
            audit_reasoning,
            final_action,
            owner_action,
            historical,
            created_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, COALESCE(%s, NOW())
        )
        RETURNING decision_id;
    """
    params = (
        tenant_id,
        scope,
        domain,
        extracted.confidence,
        extracted.situation or None,
        extracted.decision or None,
        extracted.reasoning or None,
        final_action or None,
        extracted.outcome,
        extracted.timestamp,
    )

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            conn.commit()
        if not row:
            return None
        return int(row[0])
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Extractor: failed to insert decision (tenant=%s, doc=%s): %s",
            tenant_id, document_id, e,
        )
        return None

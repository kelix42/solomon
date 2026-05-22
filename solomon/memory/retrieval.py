"""Multi-lane retrieval — Part 8 of the design doc.

When a new event lands, Solomon doesn't just look up "similar text" the way
a typical RAG system would. The design doc carves retrieval into five
parallel lanes, each surfacing a different *kind* of relevance:

    1. semantic    — pgvector cosine over decision/event embeddings
    2. recency     — the last N items in the same scope / domain
    3. entity      — prior turns with overlapping participants
    4. pressure    — prior turns with similar urgency / salience
    5. foundation  — non-negotiables + principles (the bedrock)

Each lane returns a list of (item_dict, score) tuples. Their scores are
then combined with weights and decayed by age. The top 10-15 unique items
become the retrieval context for the System-2 reasoner.

Phase 1 ships Recency, Entity, and Foundation. Semantic and Pressure are
stubbed (return empty lists) until embeddings + urgency scoring are wired
up later in the project.

Every database call is wrapped in try/except — retrieval failures must
never crash the conductor. We log and return what we have.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..storage.pool import get_pool
from ..storage.decisions import get_or_create_tenant_id

logger = logging.getLogger("solomon.memory.retrieval")

# ---------------------------------------------------------------------------
# Defaults — all overridable via adapter.get_config('solomon.<key>', default)
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS: Dict[str, float] = {
    "semantic": 0.30,
    "recency": 0.20,
    "entity": 0.25,
    "pressure": 0.15,
    "foundation": 0.10,
}

DEFAULT_DECAY_RATE = 0.02       # per-day exponential decay
DEFAULT_TOP_K = 12              # final item count, target 10-15
DEFAULT_LANE_LIMIT = 20         # per-lane pre-merge cap
DEFAULT_RECENCY_N = 10          # last N items in scope for recency lane

FOUNDATION_DIR = Path.home() / ".hermes" / "solomon" / "foundation"
FOUNDATION_FILES = ("principles.yaml", "non_negotiables.yaml")


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class RetrievalContext:
    """What MultiLaneRetrieval.retrieve() hands back to the conductor.

    `items` is the final merged + ranked list of decision rows (dicts).
    `lanes` is the names of lanes that contributed at least one item — used
    by the decision log so we can later analyze which lanes were useful for
    which decision types.
    `heuristic_ids` are the IDs of any explicit heuristics that fired
    (placeholder for Phase 2 heuristic engine integration).
    `foundation_files` are absolute paths to YAML files the reasoner
    should read in full as authoritative constraints.
    """
    items: List[Dict[str, Any]] = field(default_factory=list)
    lanes: List[str] = field(default_factory=list)
    heuristic_ids: List[str] = field(default_factory=list)
    foundation_files: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The retrieval engine
# ---------------------------------------------------------------------------

class MultiLaneRetrieval:
    """Five-lane retrieval orchestrator. Stateless apart from the adapter
    reference, so safe to instantiate per turn or reuse across turns.
    """

    LANES = ("semantic", "recency", "entity", "pressure", "foundation")

    def __init__(self, adapter) -> None:  # noqa: ANN001
        self.adapter = adapter

    # -- public ------------------------------------------------------------

    def retrieve(
        self,
        raw_event,  # noqa: ANN001  (RawEvent from solomon.capture.raw_event)
        scope: Optional[str],
        domain: Optional[str],
    ) -> RetrievalContext:
        """Run all five lanes and merge their results.

        Never raises. On any failure, returns whatever was built so far
        and logs the cause.
        """
        try:
            tenant_id = get_or_create_tenant_id()
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not resolve tenant_id for retrieval: %s", e)
            tenant_id = os.getenv("SOLOMON_TENANT_ID", "default")

        weights = self._resolve_weights()
        decay_rate = float(
            self.adapter.get_config("solomon.decay_rate", DEFAULT_DECAY_RATE)
            or DEFAULT_DECAY_RATE
        )
        top_k = int(self.adapter.get_config("solomon.retrieval_top_k", DEFAULT_TOP_K) or DEFAULT_TOP_K)

        # Run each lane defensively.
        lane_results: Dict[str, List[Tuple[Dict[str, Any], float]]] = {}
        for name, fn in (
            ("semantic", self._semantic_lane),
            ("recency", self._recency_lane),
            ("entity", self._entity_lane),
            ("pressure", self._pressure_lane),
        ):
            try:
                lane_results[name] = fn(tenant_id, raw_event, scope, domain) or []
            except Exception as e:  # noqa: BLE001
                logger.warning("Retrieval lane '%s' failed: %s", name, e)
                lane_results[name] = []

        # Foundation is special — file-based, no DB.
        try:
            foundation_paths = self._foundation_lane()
        except Exception as e:  # noqa: BLE001
            logger.warning("Foundation lane failed: %s", e)
            foundation_paths = []

        merged = self._merge_and_score(lane_results, weights, decay_rate, top_k)
        contributing_lanes = sorted({lane for lane, items in lane_results.items() if items})
        if foundation_paths:
            contributing_lanes.append("foundation")

        return RetrievalContext(
            items=merged,
            lanes=contributing_lanes,
            heuristic_ids=[],  # populated by the Phase 2 heuristic engine
            foundation_files=foundation_paths,
        )

    # -- lane: semantic ----------------------------------------------------

    def _semantic_lane(
        self,
        tenant_id: str,
        raw_event,  # noqa: ANN001
        scope: Optional[str],
        domain: Optional[str],
    ) -> List[Tuple[Dict[str, Any], float]]:
        # TODO(phase-2): embed raw_event.raw_content, run pgvector cosine
        # against decisions.embedding (or a sibling embeddings table) and
        # return the top-N with similarity scores in [0, 1]. Requires the
        # embeddings backfill job to be running first; without embeddings
        # this lane has nothing to score against.
        return []

    # -- lane: recency -----------------------------------------------------

    def _recency_lane(
        self,
        tenant_id: str,
        raw_event,  # noqa: ANN001
        scope: Optional[str],
        domain: Optional[str],
    ) -> List[Tuple[Dict[str, Any], float]]:
        """Last N decisions in the same scope (and domain, if provided).

        Score = 1.0 for the most recent item, linearly tapering to ~0.5
        for the oldest item we return. The merge step then applies
        time-decay on top of this lane score using created_at.
        """
        n = int(
            self.adapter.get_config("solomon.recency_n", DEFAULT_RECENCY_N)
            or DEFAULT_RECENCY_N
        )
        limit = max(n, DEFAULT_LANE_LIMIT)

        conditions = ["tenant_id = %s"]
        params: List[Any] = [tenant_id]
        if scope:
            conditions.append("scope = %s")
            params.append(scope)
        if domain:
            conditions.append("domain = %s")
            params.append(domain)
        where_clause = " AND ".join(conditions)

        sql = (
            "SELECT decision_id, tenant_id, scope, domain, decision_type, "
            "salience_score, final_action, created_at "
            "FROM decisions "
            f"WHERE {where_clause} "
            "ORDER BY created_at DESC "
            "LIMIT %s;"
        )
        params.append(limit)

        rows = self._fetch(sql, tuple(params))
        results: List[Tuple[Dict[str, Any], float]] = []
        total = len(rows)
        for i, row in enumerate(rows):
            # Linear taper from 1.0 -> 0.5 across the returned slice.
            if total <= 1:
                lane_score = 1.0
            else:
                lane_score = 1.0 - (0.5 * (i / (total - 1)))
            results.append((row, lane_score))
        return results

    # -- lane: entity ------------------------------------------------------

    def _entity_lane(
        self,
        tenant_id: str,
        raw_event,  # noqa: ANN001
        scope: Optional[str],
        domain: Optional[str],
    ) -> List[Tuple[Dict[str, Any], float]]:
        """Prior decisions whose raw_event participants overlap with the
        current event's participants. Scored by overlap fraction.
        """
        participants = list(getattr(raw_event, "participants", []) or [])
        if not participants:
            return []

        # Postgres JSON containment: raw_events.participants ?| array['a','b']
        # returns rows where participants jsonb contains ANY of the strings.
        sql = (
            "SELECT d.decision_id, d.tenant_id, d.scope, d.domain, d.decision_type, "
            "d.salience_score, d.final_action, d.created_at, r.participants "
            "FROM decisions d "
            "JOIN raw_events r ON r.event_id = d.event_id "
            "WHERE d.tenant_id = %s "
            "  AND r.participants ?| %s::text[] "
            "ORDER BY d.created_at DESC "
            "LIMIT %s;"
        )
        rows = self._fetch(sql, (tenant_id, participants, DEFAULT_LANE_LIMIT))

        current_set = set(str(p) for p in participants)
        results: List[Tuple[Dict[str, Any], float]] = []
        for row in rows:
            row_participants = row.pop("participants", None) or []
            if isinstance(row_participants, str):
                # Defensive: in case the driver returned the raw JSON string.
                try:
                    import json as _json
                    row_participants = _json.loads(row_participants)
                except Exception:  # noqa: BLE001
                    row_participants = []
            row_set = set(str(p) for p in row_participants)
            if not row_set:
                continue
            overlap = len(current_set & row_set)
            if overlap == 0:
                continue
            # Jaccard-ish: overlap / union, bounded in (0, 1].
            union = len(current_set | row_set) or 1
            lane_score = overlap / union
            results.append((row, lane_score))
        return results

    # -- lane: pressure ----------------------------------------------------

    def _pressure_lane(
        self,
        tenant_id: str,
        raw_event,  # noqa: ANN001
        scope: Optional[str],
        domain: Optional[str],
    ) -> List[Tuple[Dict[str, Any], float]]:
        # TODO(phase-2): once the urgency/pressure scorer lands in
        # solomon.salience, fetch decisions whose salience_score (or a
        # dedicated pressure column) falls in a similar band to the current
        # event's pressure. Will need either the current event's pressure
        # pre-computed or a quick re-score here.
        return []

    # -- lane: foundation --------------------------------------------------

    def _foundation_lane(self) -> List[str]:
        """Return absolute paths to foundation YAML files that exist on disk.

        The reasoner reads these in full and treats them as hard constraints,
        not weighted hints. We don't try to parse them here — that's the
        consumer's job. We just confirm existence and hand back the paths.
        """
        configured_dir = self.adapter.get_config(
            "solomon.foundation_dir", str(FOUNDATION_DIR)
        )
        try:
            foundation_dir = Path(configured_dir).expanduser()
        except Exception:  # noqa: BLE001
            foundation_dir = FOUNDATION_DIR

        paths: List[str] = []
        if not foundation_dir.exists():
            logger.debug(
                "Foundation directory %s does not exist; foundation lane empty.",
                foundation_dir,
            )
            return paths

        for filename in FOUNDATION_FILES:
            p = foundation_dir / filename
            if p.exists() and p.is_file():
                paths.append(str(p.resolve()))
            else:
                logger.debug("Foundation file missing: %s", p)
        return paths

    # -- merge -------------------------------------------------------------

    def _merge_and_score(
        self,
        lane_results: Dict[str, List[Tuple[Dict[str, Any], float]]],
        weights: Dict[str, float],
        decay_rate: float,
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """Combine lane outputs, dedupe by decision_id, apply time decay,
        and return the top_k items sorted by final score.
        """
        # decision_id -> {"item": dict, "score": float, "lanes": set}
        bucket: Dict[Any, Dict[str, Any]] = {}
        now = datetime.now(timezone.utc)

        for lane_name, lane_items in lane_results.items():
            weight = weights.get(lane_name, 0.0)
            if weight <= 0 or not lane_items:
                continue
            for item, lane_score in lane_items:
                days_since = self._days_since(item.get("created_at"), now)
                decay = math.exp(-decay_rate * days_since)
                contribution = weight * float(lane_score) * decay

                dec_id = item.get("decision_id")
                if dec_id is None:
                    # Without a stable key we can't dedupe; skip rather than
                    # risk double-counting.
                    continue
                slot = bucket.get(dec_id)
                if slot is None:
                    bucket[dec_id] = {
                        "item": item,
                        "score": contribution,
                        "lanes": {lane_name},
                    }
                else:
                    slot["score"] += contribution
                    slot["lanes"].add(lane_name)

        ranked = sorted(bucket.values(), key=lambda s: s["score"], reverse=True)
        final: List[Dict[str, Any]] = []
        for slot in ranked[:top_k]:
            enriched = dict(slot["item"])
            enriched["_score"] = slot["score"]
            enriched["_lanes"] = sorted(slot["lanes"])
            final.append(enriched)
        return final

    # -- helpers -----------------------------------------------------------

    def _resolve_weights(self) -> Dict[str, float]:
        weights: Dict[str, float] = {}
        for lane in self.LANES:
            default = DEFAULT_WEIGHTS.get(lane, 0.0)
            try:
                val = self.adapter.get_config(
                    f"solomon.retrieval_weights.{lane}", default
                )
                weights[lane] = float(val) if val is not None else default
            except Exception:  # noqa: BLE001
                weights[lane] = default
        return weights

    @staticmethod
    def _days_since(created_at: Any, now: datetime) -> float:
        if created_at is None:
            return 0.0
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError:
                return 0.0
        if not isinstance(created_at, datetime):
            return 0.0
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        delta = now - created_at
        return max(delta.total_seconds() / 86400.0, 0.0)

    @staticmethod
    def _fetch(sql: str, params: Tuple[Any, ...]) -> List[Dict[str, Any]]:
        """Run a SELECT and return rows as list of dicts. Never raises —
        DB errors are logged and an empty list is returned.
        """
        try:
            with get_pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    cols = [d[0] for d in (cur.description or [])]
                    return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:  # noqa: BLE001
            logger.warning("Retrieval query failed: %s | sql=%s", e, sql.split("\n")[0])
            return []

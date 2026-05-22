"""Part 26 Stage 5: heuristic mining over an ingestion batch.

After every document in a job has been chunked, classified, extracted, and
embedded, we make one cross-document pass: pull every historical decision
this batch produced, group them by scope, and ask the deep LLM to spot
repeated patterns that look like implicit rules.

Example from the design doc:
    "In 23 of the 31 extracted pricing decisions, after-hours work was
     charged 20-25% above base. This looks like an implicit rule."

Mined patterns land in the ``pending_heuristics`` table for owner review.
Nothing here promotes a heuristic to ``heuristics`` automatically — that is
the owner's explicit decision, mediated by the approvals UI elsewhere. This
module's job is only to surface candidates.

Everything is best-effort. Any error path returns an empty list rather than
crashing the ingestion job: heuristic mining is the icing, not the cake.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..reasoning.llm import get_client
from ..storage.pool import get_pool

logger = logging.getLogger("solomon.ingestion.miner")


# Minimum number of decisions in a single scope before we bother asking the
# LLM to look for patterns. Five is what the design doc specifies as the
# floor for "this is a pattern, not a coincidence".
MIN_DECISIONS_PER_SCOPE = 5

# Hard cap on how many decisions we'll cram into a single LLM prompt for one
# scope. Beyond this we sample. The deep tier can in principle take more,
# but the marginal signal drops off and the cost climbs linearly.
MAX_DECISIONS_PER_PROMPT = 80


@dataclass
class MinedHeuristic:
    """One candidate rule produced by the cross-document miner."""

    scope: str
    proposed_condition: str
    proposed_action: str
    support_count: int
    evidence_decision_ids: List[int] = field(default_factory=list)
    confidence: float = 0.5


# ---------------------------------------------------------------------------
# Budget guard
# ---------------------------------------------------------------------------
# TODO: A budget_tracker module is planned for Part 16 (nightly cycle) and
# Part 26 (ingestion). When it exists, replace this stub with a real check
# against the tenant's remaining daily token budget. Until then we always
# allow the miner to run — the cost per batch is bounded by
# MAX_DECISIONS_PER_PROMPT × scope_count anyway.
def _budget_allows(tenant_id: str) -> bool:
    try:
        from .. import budget_tracker  # type: ignore
    except ImportError:
        return True
    try:
        return bool(budget_tracker.allows(tenant_id, job="heuristic_miner"))  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        logger.debug("budget_tracker check failed (%s); proceeding anyway", e)
        return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def mine_batch(tenant_id: str, job_id: int) -> List[MinedHeuristic]:
    """Mine cross-document heuristics for one ingestion job.

    We resolve the job's ``started_at`` and treat any historical decision
    created at or after that timestamp for this tenant as belonging to the
    batch. Decisions don't carry a direct ``job_id`` foreign key today, so
    this timestamp window is the simplest reliable join.

    Returns the list of mined candidates (also written to
    ``pending_heuristics``). Returns ``[]`` on any error.
    """
    if not _budget_allows(tenant_id):
        logger.info("Heuristic mining skipped for tenant=%s (budget exceeded).", tenant_id)
        return []

    try:
        started_at = _read_job_started_at(job_id, tenant_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not read ingestion_jobs row job_id=%s: %s", job_id, e)
        return []
    if started_at is None:
        logger.info("Job %s has no started_at yet; skipping mining.", job_id)
        return []

    try:
        decisions_by_scope = _load_decisions_by_scope(tenant_id, started_at)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed loading decisions for mining (job=%s): %s", job_id, e)
        return []

    if not decisions_by_scope:
        logger.info("No historical decisions in batch for tenant=%s job=%s.", tenant_id, job_id)
        return []

    client = get_client()
    if not client.configured:
        logger.info("LLM not configured; skipping heuristic mining.")
        return []

    mined: List[MinedHeuristic] = []
    for scope, rows in decisions_by_scope.items():
        if len(rows) < MIN_DECISIONS_PER_SCOPE:
            continue
        try:
            patterns = _mine_scope(client, scope, rows)
        except Exception as e:  # noqa: BLE001
            logger.warning("Mining failed for scope=%s: %s", scope, e)
            continue
        for p in patterns:
            try:
                pid = store_mined(tenant_id, scope, p)
                if pid is not None:
                    mined.append(p)
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not persist mined heuristic in scope=%s: %s", scope, e)

    logger.info(
        "Heuristic mining done: tenant=%s job=%s scopes=%d mined=%d",
        tenant_id, job_id, len(decisions_by_scope), len(mined),
    )
    return mined


def store_mined(tenant_id: str, scope: str, mined: MinedHeuristic) -> Optional[int]:
    """Insert one MinedHeuristic into ``pending_heuristics``.

    Returns the new ``pending_id`` or None on failure.
    """
    try:
        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pending_heuristics (
                        tenant_id, scope, proposed_condition, proposed_action,
                        source, support_count, evidence_list, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    RETURNING pending_id;
                    """,
                    (
                        tenant_id,
                        scope,
                        mined.proposed_condition,
                        mined.proposed_action,
                        "ingestion_miner",
                        int(mined.support_count),
                        json.dumps({
                            "decision_ids": list(mined.evidence_decision_ids),
                            "confidence": float(mined.confidence),
                        }),
                        "pending",
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return int(row[0]) if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning("store_mined failed (scope=%s): %s", scope, e)
        return None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _read_job_started_at(job_id: int, tenant_id: str):
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT started_at FROM ingestion_jobs WHERE job_id = %s AND tenant_id = %s;",
                (job_id, tenant_id),
            )
            row = cur.fetchone()
    if not row:
        return None
    return row[0]


def _load_decisions_by_scope(tenant_id: str, started_at) -> Dict[str, List[Dict[str, Any]]]:
    """Pull historical decisions in the batch, grouped by scope."""
    pool = get_pool()
    out: Dict[str, List[Dict[str, Any]]] = {}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT decision_id, scope, domain, decision_type,
                       proposed_action, final_action, system_2_answer
                FROM decisions
                WHERE tenant_id = %s
                  AND historical = TRUE
                  AND created_at >= %s
                  AND scope IS NOT NULL
                ORDER BY scope, decision_id;
                """,
                (tenant_id, started_at),
            )
            for did, scope, domain, dtype, prop, final, s2 in cur.fetchall():
                out.setdefault(scope, []).append({
                    "decision_id": int(did),
                    "domain": domain,
                    "decision_type": dtype,
                    "proposed_action": prop,
                    "final_action": final,
                    "summary": s2,
                })
    return out


def _mine_scope(client, scope: str, rows: List[Dict[str, Any]]) -> List[MinedHeuristic]:
    """Ask the deep LLM to find recurring patterns in one scope's decisions."""
    # If there are too many, sample evenly. The miner just needs enough
    # diversity to spot a pattern, not the full universe.
    if len(rows) > MAX_DECISIONS_PER_PROMPT:
        step = len(rows) / MAX_DECISIONS_PER_PROMPT
        sampled = [rows[int(i * step)] for i in range(MAX_DECISIONS_PER_PROMPT)]
    else:
        sampled = rows

    # Compact JSON-ish rendering keeps prompt size low while preserving the
    # decision_id so the model can cite evidence accurately.
    rendered = "\n".join(
        f"- id={d['decision_id']} type={d.get('decision_type') or '?'} "
        f"domain={d.get('domain') or '?'} :: "
        f"action={(d.get('final_action') or d.get('proposed_action') or d.get('summary') or '').strip()[:300]}"
        for d in sampled
    )

    prompt = (
        f"Here are {len(sampled)} business decisions in scope '{scope}'.\n"
        f"Look for repeated patterns that suggest implicit rules the business "
        f"is following but has not written down.\n\n"
        f"DECISIONS:\n{rendered}\n\n"
        f"Return JSON of the form:\n"
        f"{{\"patterns\": [{{\"condition\": str, \"action\": str, "
        f"\"support_count\": int, \"evidence_decision_ids\": list[int], "
        f"\"confidence\": float}}]}}\n"
        f"Only include patterns supported by 5 or more decisions. "
        f"evidence_decision_ids must be drawn from the ids listed above. "
        f"If you find nothing, return {{\"patterns\": []}}."
    )

    resp = client.call(
        tier="deep",
        system=(
            "You are an analyst spotting implicit rules in a business's past "
            "decisions. Be conservative. Only surface patterns that are "
            "clearly recurring. Return strict JSON."
        ),
        user=prompt,
        json_mode=True,
        max_tokens=2048,
        temperature=0.2,
    )
    parsed = client.parse_json(resp.text) or {}
    patterns = parsed.get("patterns") or []
    valid_ids = {d["decision_id"] for d in sampled}

    out: List[MinedHeuristic] = []
    for p in patterns:
        try:
            cond = str(p.get("condition") or "").strip()
            action = str(p.get("action") or "").strip()
            if not cond or not action:
                continue
            evidence = [int(x) for x in (p.get("evidence_decision_ids") or []) if _is_intish(x)]
            evidence = [e for e in evidence if e in valid_ids]
            support = int(p.get("support_count") or len(evidence))
            if support < MIN_DECISIONS_PER_SCOPE:
                continue
            conf = float(p.get("confidence", 0.5) or 0.5)
            conf = max(0.0, min(1.0, conf))
            out.append(MinedHeuristic(
                scope=scope,
                proposed_condition=cond,
                proposed_action=action,
                support_count=support,
                evidence_decision_ids=evidence,
                confidence=conf,
            ))
        except Exception as e:  # noqa: BLE001
            logger.debug("Skipping malformed pattern in scope=%s: %s", scope, e)
            continue
    return out


def _is_intish(x: Any) -> bool:
    try:
        int(x)
        return True
    except (TypeError, ValueError):
        return False

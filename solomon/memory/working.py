"""Working memory — Part 7 of the Solomon design doc.

Working memory is a small, fast cache of currently-active items:
open deals, last 7 days of decisions, active mentoring topics, and
recently elevated heuristics. It's the "what's on my desk right now"
layer that sits in front of long-term retrieval.

Design rules from Part 7:
  - Cap ~50 items per tenant.
  - TTL 7 days (14 days when SOLOMON_VACATION_MODE is set, so the owner
    doesn't return from a break to an empty desk).
  - Eviction: lowest-salience-oldest first.
  - Postgres-backed (`working_memory` table). Redis is optional; the
    Postgres TTL path is good enough for v1 and keeps the dependency
    surface smaller.

This module never raises into the conductor — DB errors are logged and
swallowed. Working memory missing is a degraded experience, not a crash.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..storage.decisions import get_or_create_tenant_id
from ..storage.pool import get_pool

logger = logging.getLogger("solomon.memory.working")

# Soft cap from the design doc. We evict down to this size after each
# write so the table stays small and the SELECT stays cheap.
_MAX_ITEMS_PER_TENANT = 50

# Max characters we keep of the System-2 answer in the WM payload. WM is
# meant to be a *summary* layer; the full answer lives in `decisions`.
_S2_TRUNCATE_CHARS = 400


def _ttl_days() -> int:
    """Return the WM TTL in days. Honors SOLOMON_VACATION_MODE."""
    if os.getenv("SOLOMON_VACATION_MODE"):
        return 14
    return 7


@dataclass
class HotContext:
    """The slice of working memory handed to the reasoning layer for a turn.

    `items` is a list of dicts; each dict is the JSON payload that was
    stored when the originating turn finished, plus a couple of
    bookkeeping fields (`wm_key`, `salience`, `last_touched_at` as ISO
    strings) so callers can reason about freshness if they want.
    """

    items: List[Dict[str, Any]] = field(default_factory=list)

    def is_thin(self) -> bool:
        """True when there's basically nothing on the desk.

        The conductor uses this to flip `working_memory_used` on the
        decision row — if we only have 0 or 1 items we shouldn't claim
        WM materially shaped the answer.
        """
        return len(self.items) < 2


class WorkingMemory:
    """Postgres-backed working memory cache (Part 7).

    Keyed by (tenant_id, wm_key). The wm_key for a turn is
    ``scope:<scope>:<event_id>`` so successive events in the same scope
    accumulate distinct rows but the same event re-processed updates in
    place.
    """

    def __init__(self, adapter) -> None:  # noqa: ANN001
        self.adapter = adapter

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------
    def fetch(self, scope: Optional[str], raw_event) -> HotContext:  # noqa: ANN001
        """Pull the active items for `scope` from the WM table.

        Ordered by last_touched_at DESC, limit 20, and filtered to rows
        whose expires_at is still in the future. If scope is None we
        return rows from any scope (still tenant-scoped) — better to
        hand the reasoner *something* than nothing.
        """
        try:
            tenant_id = get_or_create_tenant_id()
        except Exception as e:  # noqa: BLE001
            logger.warning("WM fetch: could not resolve tenant_id: %s", e)
            return HotContext(items=[])

        try:
            with get_pool().connection() as conn:
                with conn.cursor() as cur:
                    if scope:
                        cur.execute(
                            """
                            SELECT wm_key, payload, salience, last_touched_at
                              FROM working_memory
                             WHERE tenant_id = %s
                               AND expires_at > NOW()
                               AND wm_key LIKE %s
                             ORDER BY last_touched_at DESC
                             LIMIT 20;
                            """,
                            (tenant_id, f"scope:{scope}:%"),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT wm_key, payload, salience, last_touched_at
                              FROM working_memory
                             WHERE tenant_id = %s
                               AND expires_at > NOW()
                             ORDER BY last_touched_at DESC
                             LIMIT 20;
                            """,
                            (tenant_id,),
                        )
                    rows = cur.fetchall() or []
        except Exception as e:  # noqa: BLE001
            logger.warning("WM fetch failed (scope=%s): %s", scope, e)
            return HotContext(items=[])

        items: List[Dict[str, Any]] = []
        for wm_key, payload, salience, last_touched_at in rows:
            item: Dict[str, Any] = {}
            if isinstance(payload, dict):
                item.update(payload)
            elif isinstance(payload, str):
                try:
                    item.update(json.loads(payload))
                except Exception:  # noqa: BLE001
                    item["payload_raw"] = payload
            item["wm_key"] = wm_key
            try:
                item["salience"] = float(salience) if salience is not None else None
            except Exception:  # noqa: BLE001
                item["salience"] = None
            item["last_touched_at"] = (
                last_touched_at.isoformat() if hasattr(last_touched_at, "isoformat") else last_touched_at
            )
            items.append(item)

        return HotContext(items=items)

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------
    def update_after_turn(self, turn) -> None:  # noqa: ANN001  (TurnContext)
        """Upsert one row representing this turn into working_memory.

        Called after the conductor has finished a turn. We store a small
        dict — scope, domain, decision_type, salience, and a truncated
        System-2 answer — so the next turn in the same scope can see a
        compressed trace of what we just decided.
        """
        raw_event = getattr(turn, "raw_event", None)
        if raw_event is None or getattr(raw_event, "id", None) is None:
            # Nothing to key on; skip silently. WM is best-effort.
            return

        scope = getattr(turn, "scope", None) or "unknown"
        wm_key = f"scope:{scope}:{raw_event.id}"

        s2 = getattr(turn, "system_2_answer", None)
        s2_trunc: Optional[str]
        if isinstance(s2, str) and len(s2) > _S2_TRUNCATE_CHARS:
            s2_trunc = s2[:_S2_TRUNCATE_CHARS]
        else:
            s2_trunc = s2

        payload: Dict[str, Any] = {
            "scope": scope,
            "domain": getattr(turn, "domain", None),
            "decision_type": getattr(turn, "decision_type", None),
            "salience": getattr(turn, "salience_score", None),
            "system_2_answer": s2_trunc,
        }

        salience = getattr(turn, "salience_score", None)
        try:
            salience_val = float(salience) if salience is not None else 0.5
        except (TypeError, ValueError):
            salience_val = 0.5

        ttl_days = _ttl_days()

        try:
            tenant_id = get_or_create_tenant_id()
        except Exception as e:  # noqa: BLE001
            logger.warning("WM update: could not resolve tenant_id: %s", e)
            return

        try:
            with get_pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO working_memory (
                            wm_key, tenant_id, payload, salience,
                            last_touched_at, expires_at
                        ) VALUES (
                            %s, %s, %s::jsonb, %s,
                            NOW(), NOW() + (%s || ' days')::interval
                        )
                        ON CONFLICT (tenant_id, wm_key) DO UPDATE SET
                            payload         = EXCLUDED.payload,
                            salience        = EXCLUDED.salience,
                            last_touched_at = NOW(),
                            expires_at      = EXCLUDED.expires_at;
                        """,
                        (
                            wm_key,
                            tenant_id,
                            json.dumps(payload, default=str),
                            salience_val,
                            str(ttl_days),
                        ),
                    )
                    # Evict: keep newest+highest-salience 50, drop the
                    # lowest-salience-oldest first. Expired rows go too.
                    cur.execute(
                        "DELETE FROM working_memory "
                        " WHERE tenant_id = %s AND expires_at <= NOW();",
                        (tenant_id,),
                    )
                    cur.execute(
                        """
                        DELETE FROM working_memory
                         WHERE tenant_id = %s
                           AND wm_key IN (
                               SELECT wm_key FROM working_memory
                                WHERE tenant_id = %s
                                ORDER BY salience ASC, last_touched_at ASC
                                OFFSET %s
                           );
                        """,
                        (tenant_id, tenant_id, _MAX_ITEMS_PER_TENANT),
                    )
                conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("WM update failed (key=%s): %s", wm_key, e)
            return

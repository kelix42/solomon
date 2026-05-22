"""Job 6 — Working memory cleanup (Part 16).

Evict items that have not been touched within their TTL. After eviction,
if the cache is still over the 50-item cap, evict lowest-salience-oldest
until size = 50. Open-thread items override the TTL (kept regardless).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict

logger = logging.getLogger("solomon.sleep.job_6")


def run(*, tenant_id: str, **kwargs: Any) -> Dict[str, Any]:
    from ..storage.pool import get_pool

    cap = 50
    evicted = 0

    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                # Drop expired items, unless they're tied to an open thread.
                # For Phase 1 we don't yet link open_items <-> wm_key; this
                # just trims by expires_at.
                cur.execute(
                    "DELETE FROM working_memory "
                    "WHERE tenant_id=%s AND expires_at < NOW();",
                    (tenant_id,),
                )
                evicted_expired = cur.rowcount or 0

                # Then enforce cap.
                cur.execute(
                    "SELECT COUNT(*) FROM working_memory WHERE tenant_id=%s;",
                    (tenant_id,),
                )
                row = cur.fetchone()
                count_after = int(row[0] or 0)
                evicted_cap = 0
                if count_after > cap:
                    over = count_after - cap
                    cur.execute(
                        """
                        DELETE FROM working_memory
                        WHERE (tenant_id, wm_key) IN (
                            SELECT tenant_id, wm_key
                            FROM working_memory
                            WHERE tenant_id=%s
                            ORDER BY salience ASC, last_touched_at ASC
                            LIMIT %s
                        );
                        """,
                        (tenant_id, over),
                    )
                    evicted_cap = cur.rowcount or 0
                evicted = evicted_expired + evicted_cap
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("Job 6 working memory cleanup failed: %s", e)

    return {"items_processed": evicted, "tokens": 0}

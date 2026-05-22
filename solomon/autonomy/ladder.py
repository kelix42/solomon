"""Autonomy ladder — per-scope autonomy levels (Part 11 of the design doc).

Each scope (e.g. ``scheduling``, ``billing``, ``hiring``) advances independently
through four rungs:

* ``watch`` — audit gate's verdict is logged but no action is taken. Brain
  observes only.
* ``suggest`` — audit-approved actions show up in the owner's inbox as drafts.
* ``act_with_approval`` — audit-approved actions queue for one-click owner
  approval with a 24h TTL.
* ``act_alone`` — audit-approved actions execute immediately; owner gets a
  digest.

New tenants start *all* scopes at ``watch`` for the first 30 days
(observe-only mode), regardless of any per-scope rows. The promotion and
demotion logic itself lives in ``solomon/sleep/job_7_autonomy.py``; this
module is a thin, crash-proof reader/writer over the ``autonomy_state``
table.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..storage.decisions import get_or_create_tenant_id
from ..storage.pool import get_pool

logger = logging.getLogger("solomon.autonomy")

AUTONOMY_LEVELS = ("watch", "suggest", "act_with_approval", "act_alone")

OBSERVE_ONLY_DAYS = 30
_DEFAULT_LEVEL = "watch"


class AutonomyLadder:
    """Reads and writes the per-scope autonomy level for the active tenant."""

    def __init__(self, adapter) -> None:  # noqa: ANN001
        self.adapter = adapter

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def level_for(self, scope: Optional[str]) -> str:
        """Return the autonomy level for ``scope``.

        Defaults to ``'watch'`` for unknown/new scopes, ``None`` scope, or any
        database error. Never raises.
        """
        if not scope:
            return _DEFAULT_LEVEL

        tenant_id = get_or_create_tenant_id()
        try:
            with get_pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT level FROM autonomy_state "
                        "WHERE tenant_id = %s AND scope = %s;",
                        (tenant_id, scope),
                    )
                    row = cur.fetchone()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "level_for(%s) failed, defaulting to '%s': %s",
                scope, _DEFAULT_LEVEL, e,
            )
            return _DEFAULT_LEVEL

        if not row or not row[0]:
            return _DEFAULT_LEVEL

        level = str(row[0])
        if level not in AUTONOMY_LEVELS:
            logger.warning(
                "Unknown autonomy level '%s' for scope=%s; defaulting to '%s'",
                level, scope, _DEFAULT_LEVEL,
            )
            return _DEFAULT_LEVEL
        return level

    def is_observe_only(self) -> bool:
        """True if the tenant is still within the 30-day observe-only window.

        Looks at ``tenants.onboarded_at``. If that column is NULL (tenant
        hasn't completed onboarding yet) or is less than ``OBSERVE_ONLY_DAYS``
        in the past, the tenant is observe-only. Any DB error is treated as
        observe-only (fail safe — don't let actions fire if we can't tell).
        """
        tenant_id = get_or_create_tenant_id()
        try:
            with get_pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT onboarded_at FROM tenants WHERE tenant_id = %s;",
                        (tenant_id,),
                    )
                    row = cur.fetchone()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "is_observe_only() DB read failed for tenant=%s; "
                "treating as observe-only: %s",
                tenant_id, e,
            )
            return True

        if not row:
            # No tenant row at all — observe-only.
            return True

        onboarded_at = row[0]
        if onboarded_at is None:
            return True

        # Normalize to aware UTC.
        if isinstance(onboarded_at, datetime):
            if onboarded_at.tzinfo is None:
                onboarded_at = onboarded_at.replace(tzinfo=timezone.utc)
        else:
            # Unexpected type — fail safe.
            logger.warning(
                "onboarded_at had unexpected type %s; treating as observe-only",
                type(onboarded_at).__name__,
            )
            return True

        cutoff = datetime.now(timezone.utc) - timedelta(days=OBSERVE_ONLY_DAYS)
        return onboarded_at > cutoff

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def set_level(self, scope: str, level: str) -> None:
        """Upsert the autonomy_state row for ``(tenant, scope)``.

        Used by Job 7 (sleep cycle) and the owner UI. Errors are logged but
        never raised — autonomy changes are best-effort here; the source of
        truth retries on the next sleep cycle.
        """
        if level not in AUTONOMY_LEVELS:
            logger.error(
                "set_level: refusing invalid level '%s' for scope=%s "
                "(valid: %s)",
                level, scope, AUTONOMY_LEVELS,
            )
            return
        if not scope:
            logger.error("set_level: empty scope, ignoring")
            return

        tenant_id = get_or_create_tenant_id()
        try:
            with get_pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO autonomy_state (tenant_id, scope, level, since)
                        VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (tenant_id, scope) DO UPDATE SET
                            level = EXCLUDED.level,
                            since = NOW();
                        """,
                        (tenant_id, scope, level),
                    )
                conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "set_level(scope=%s, level=%s) failed: %s",
                scope, level, e,
            )

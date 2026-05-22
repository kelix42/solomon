"""Autonomy ladder — per-scope autonomy levels (Part 11 of the design doc).

Five rungs, L0–L4 (Drive naming, per ``references/autonomy-spectrum.md``):

================  =================  ==========================================
Level (int)       Name (str)         Behaviour
================  =================  ==========================================
``L0`` (0)        manual             Solomon does nothing automatic; only
                                     answers when asked.
``L1`` (1)        suggested          Proposes; owner approves every action.
``L2`` (2)        drafted            Drafts and ships only after one-tap.
``L3`` (3)        supervised         Ships routine actions; novel still needs
                                     approval.
``L4`` (4)        autonomous         Ships everything in scope; daily digest.
================  =================  ==========================================

Owner-state ceiling (pipeline Stage 9): per-event ceiling that caps the
scope level based on biometrics. Effective autonomy is
``min(scope_level, owner_state_ceiling(owner_state))``.

This module is a thin, crash-proof reader/writer. The promotion /
demotion logic itself lives in ``solomon/sleep/job_7_autonomy.py``.

Backwards compatibility
-----------------------
``AUTONOMY_LEVELS`` (the legacy 4-string tuple) is preserved as a
deprecated alias so ``solomon/sleep/job_7_autonomy.py`` and
``solomon/conductor.py`` continue to work this session — those files
are off-limits per the session plan. Index ordering inside the legacy
tuple matches the L0–L3 prefix of the new scheme; ``act_alone`` maps to
L3 (Supervised), not L4, because the legacy scheme had no Autonomous
rung.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from ..storage.pool import cursor, execute, get_conn

logger = logging.getLogger("solomon.autonomy")

# ---------------------------------------------------------------------------
# Canonical L0–L4 scheme
# ---------------------------------------------------------------------------

#: Display name keyed by level int (Drive's ``references/autonomy-spectrum.md``).
LEVEL_NAMES = {
    0: "manual",
    1: "suggested",
    2: "drafted",
    3: "supervised",
    4: "autonomous",
}

#: Valid level ints.
LEVEL_INTS = tuple(LEVEL_NAMES.keys())

#: Reverse lookup: name → int.
_NAME_TO_INT = {v: k for k, v in LEVEL_NAMES.items()}

# ---------------------------------------------------------------------------
# Legacy 4-string tuple (kept for backwards compat — see module docstring)
# ---------------------------------------------------------------------------

AUTONOMY_LEVELS = ("watch", "suggest", "act_with_approval", "act_alone")

OBSERVE_ONLY_DAYS = 30
_DEFAULT_LEVEL = "watch"


# ---------------------------------------------------------------------------
# Owner-state ceiling (Stage 9)
# ---------------------------------------------------------------------------

def owner_state_ceiling(state: Optional[str]) -> int:
    """Map an owner_state string to its L0–L4 ceiling.

    Drive's `references/autonomy-spectrum.md` §"Owner-state ceiling":

      - Green (recovery > 60% AND sleep > 7h): full scope autonomy → L4.
      - Yellow (recovery 33–60% OR sleep 5–7h): L2 ceiling.
      - Red (recovery < 33% OR explicit stress flag): L1 ceiling.
      - Whoop missing / stale > 24h: default Green, warn-once → L4.

    Unknown / None / unrecognised strings → L4 (no cap), matching the
    Drive "missing data defaults to green" rule.
    """
    if state is None:
        return 4
    s = str(state).strip().lower()
    if s == "green":
        return 4
    if s == "yellow":
        return 2
    if s == "red":
        return 1
    # 'unknown' and anything else → no cap.
    return 4


def effective_for(scope_level: Union[int, str], owner_state: Optional[str]) -> int:
    """Effective autonomy for one event.

    ``min(scope_level, owner_state_ceiling(owner_state))``.

    ``scope_level`` may be an int (0–4) or a string ('L0'..'L4' or one of
    the legacy 4-string names). Anything unrecognised is clamped to 0
    (manual) — fail safe.
    """
    sl = _coerce_level(scope_level)
    ceiling = owner_state_ceiling(owner_state)
    return min(sl, ceiling)


def _coerce_level(value: Union[int, str, None]) -> int:
    """Normalize various scope-level shapes into an L0–L4 int."""
    if value is None:
        return 0
    if isinstance(value, bool):  # bool is a subclass of int; guard.
        return 0
    if isinstance(value, int):
        return max(0, min(4, value))
    s = str(value).strip()
    if not s:
        return 0
    # "L3" / "l3"
    if len(s) >= 2 and s[0] in ("L", "l"):
        try:
            n = int(s[1:])
            return max(0, min(4, n))
        except ValueError:
            pass
    # "3"
    try:
        n = int(s)
        return max(0, min(4, n))
    except ValueError:
        pass
    # Display name lookup.
    s_lower = s.lower()
    if s_lower in _NAME_TO_INT:
        return _NAME_TO_INT[s_lower]
    # Legacy 4-string names → best-effort mapping.
    legacy_map = {
        "watch": 0,
        "suggest": 1,
        "act_with_approval": 2,
        "act_alone": 3,
    }
    if s_lower in legacy_map:
        return legacy_map[s_lower]
    return 0


# ---------------------------------------------------------------------------
# Scope-autonomy table reader (canonical, new code)
# ---------------------------------------------------------------------------

def scope_level(tenant_id: str, scope: Optional[str]) -> int:
    """Return the scope-autonomy level for ``(tenant_id, scope)`` as an int.

    Reads ``scope_autonomy`` (the v2 table; Drive's name). Missing row →
    L0. Any DB error → L0. Never raises.
    """
    if not scope:
        return 0
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "SELECT level FROM scope_autonomy WHERE tenant_id = ? AND scope = ?",
                    (tenant_id, scope),
                )
                row = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning("scope_level(%s,%s) failed: %s", tenant_id, scope, e)
        return 0
    if row is None:
        return 0
    raw = row[0] if not hasattr(row, "keys") else row["level"]
    return _coerce_level(raw)


# ---------------------------------------------------------------------------
# Legacy class (kept so conductor.py keeps working this session)
# ---------------------------------------------------------------------------

class AutonomyLadder:
    """Legacy reader/writer over the (deprecated) ``autonomy_state`` table.

    The current schema uses ``scope_autonomy`` and the L0–L4 int scheme;
    this class survives only so ``solomon.conductor`` continues to import
    and run. Treat every DB error as a soft default to ``watch`` (the
    legacy "observe-only" floor).
    """

    def __init__(self, adapter) -> None:  # noqa: ANN001
        self.adapter = adapter

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def level_for(self, scope: Optional[str]) -> str:
        if not scope:
            return _DEFAULT_LEVEL
        try:
            from ..storage.decisions import get_or_create_tenant_id
            tenant_id = get_or_create_tenant_id()
        except Exception as e:  # noqa: BLE001
            logger.warning("level_for: tenant id resolution failed: %s", e)
            return _DEFAULT_LEVEL
        try:
            with get_conn() as conn:
                with cursor(conn) as cur:
                    # Try the new table first; fall back to the legacy one
                    # so existing installs keep working.
                    try:
                        execute(
                            cur,
                            "SELECT level FROM scope_autonomy WHERE tenant_id = ? AND scope = ?",
                            (tenant_id, scope),
                        )
                        row = cur.fetchone()
                    except Exception:  # noqa: BLE001
                        execute(
                            cur,
                            "SELECT level FROM autonomy_state WHERE tenant_id = ? AND scope = ?",
                            (tenant_id, scope),
                        )
                        row = cur.fetchone()
        except Exception as e:  # noqa: BLE001
            logger.warning("level_for(%s) failed, defaulting to '%s': %s",
                           scope, _DEFAULT_LEVEL, e)
            return _DEFAULT_LEVEL

        if not row or not row[0]:
            return _DEFAULT_LEVEL
        raw_level = str(row[0])
        # Accept both L0..L4 ints-as-strings and legacy names.
        if raw_level in AUTONOMY_LEVELS:
            return raw_level
        # Try to translate L0..L4 back into the legacy 4-string scheme.
        if raw_level.startswith(("L", "l")):
            try:
                idx = int(raw_level[1:])
                if 0 <= idx <= 3:
                    return AUTONOMY_LEVELS[idx]
                if idx == 4:
                    return AUTONOMY_LEVELS[-1]
            except ValueError:
                pass
        logger.warning("Unknown autonomy level %r for scope=%s; defaulting to '%s'",
                       raw_level, scope, _DEFAULT_LEVEL)
        return _DEFAULT_LEVEL

    def is_observe_only(self) -> bool:
        """True if the tenant is within the 30-day observe-only window."""
        try:
            from ..storage.decisions import get_or_create_tenant_id
            tenant_id = get_or_create_tenant_id()
        except Exception as e:  # noqa: BLE001
            logger.warning("is_observe_only: tenant id resolution failed: %s", e)
            return True

        try:
            with get_conn() as conn:
                with cursor(conn) as cur:
                    execute(
                        cur,
                        "SELECT onboarded_at FROM tenants WHERE tenant_id = ?",
                        (tenant_id,),
                    )
                    row = cur.fetchone()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "is_observe_only() DB read failed for tenant=%s; treating as observe-only: %s",
                tenant_id, e,
            )
            return True

        if not row:
            return True

        onboarded_at = row[0]
        if onboarded_at is None:
            return True

        if isinstance(onboarded_at, str):
            try:
                onboarded_at = datetime.fromisoformat(onboarded_at.replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                return True
        if isinstance(onboarded_at, datetime):
            if onboarded_at.tzinfo is None:
                onboarded_at = onboarded_at.replace(tzinfo=timezone.utc)
        else:
            logger.warning("onboarded_at had unexpected type %s; treating as observe-only",
                           type(onboarded_at).__name__)
            return True

        cutoff = datetime.now(timezone.utc) - timedelta(days=OBSERVE_ONLY_DAYS)
        return onboarded_at > cutoff

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def set_level(self, scope: str, level: str) -> None:
        if level not in AUTONOMY_LEVELS:
            logger.error("set_level: refusing invalid level %r for scope=%s (valid: %s)",
                         level, scope, AUTONOMY_LEVELS)
            return
        if not scope:
            logger.error("set_level: empty scope, ignoring")
            return

        try:
            from ..storage.decisions import get_or_create_tenant_id
            tenant_id = get_or_create_tenant_id()
        except Exception as e:  # noqa: BLE001
            logger.warning("set_level: tenant id resolution failed: %s", e)
            return

        # Portable upsert: delete-then-insert.
        try:
            with get_conn() as conn:
                with cursor(conn) as cur:
                    try:
                        execute(
                            cur,
                            "DELETE FROM scope_autonomy WHERE tenant_id = ? AND scope = ?",
                            (tenant_id, scope),
                        )
                        execute(
                            cur,
                            "INSERT INTO scope_autonomy (tenant_id, scope, level, since) "
                            "VALUES (?, ?, ?, datetime('now'))",
                            (tenant_id, scope, level),
                        )
                    except Exception:  # noqa: BLE001
                        # Fall back to the legacy table if scope_autonomy is absent.
                        execute(
                            cur,
                            "DELETE FROM autonomy_state WHERE tenant_id = ? AND scope = ?",
                            (tenant_id, scope),
                        )
                        execute(
                            cur,
                            "INSERT INTO autonomy_state (tenant_id, scope, level, since) "
                            "VALUES (?, ?, ?, datetime('now'))",
                            (tenant_id, scope, level),
                        )
                conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("set_level(scope=%s, level=%s) failed: %s", scope, level, e)

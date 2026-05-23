"""Session lifecycle helpers for skill-driven onboarding.

Wraps the existing v1 helpers (``_ensure_tenant``, ``_open_or_resume_session``,
``_render_foundation``) and adds a per-process registry mapping
``hermes_session_id -> active onboarding session row``. The conductor
reads this registry on every turn to decide whether to inject the skill.

Storage stays canonical: the ``sessions`` table is the source of truth.
The registry is rebuilt from ``status='open'`` rows on Conductor startup
so a Hermes restart mid-interview doesn't lose context.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..storage.pool import cursor, execute, get_conn, jsonify, parse_json

logger = logging.getLogger("solomon.onboarding_v2.session")

# Map from "session_key" (the slash-command arg) -> probe library domain + ordinal.
# Mirrors the seven sessions in the Drive umbrella skill.
SESSION_KEY_TO_DOMAIN: Dict[str, Tuple[str, int]] = {
    "session_0": ("industry", 0),
    "session_1": ("belief_system", 1),
    "session_2": ("why", 2),
    "session_3": ("principles", 3),
    "session_4": ("ideal_outcomes", 4),
    "session_5": ("non_negotiables", 5),
    "session_6": ("scopes", 6),
}

# Map domain -> skill name (the SKILL.md the LLM should load and follow).
DOMAIN_TO_SKILL: Dict[str, str] = {
    "industry": "solomon-onboarding-00-industry",
    "belief_system": "solomon-onboarding-01-belief-system",
    "why": "solomon-onboarding-02-why",
    "principles": "solomon-onboarding-03-principles",
    "ideal_outcomes": "solomon-onboarding-04-ideal-outcomes",
    "non_negotiables": "solomon-onboarding-05-non-negotiables",
    "scopes": "solomon-onboarding-06-scopes",
}


@dataclass
class ActiveInterview:
    """One open interview attached to a Hermes session_id."""
    db_session_id: str        # row id in solomon.sessions
    domain: str               # 'industry', 'belief_system', ...
    ordinal: int              # 0..6
    skill_name: str           # 'solomon-onboarding-00-industry'
    tenant_id: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class OnboardingSessionRegistry:
    """Thread-safe map of hermes_session_id -> ActiveInterview."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: Dict[str, ActiveInterview] = {}

    def register(self, hermes_session_id: str, iv: ActiveInterview) -> None:
        with self._lock:
            self._active[hermes_session_id] = iv

    def get(self, hermes_session_id: str) -> Optional[ActiveInterview]:
        with self._lock:
            return self._active.get(hermes_session_id)

    def clear(self, hermes_session_id: str) -> Optional[ActiveInterview]:
        with self._lock:
            return self._active.pop(hermes_session_id, None)

    def is_active(self, hermes_session_id: str) -> bool:
        return self.get(hermes_session_id) is not None

    def all_active(self) -> Dict[str, ActiveInterview]:
        with self._lock:
            return dict(self._active)


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def ensure_tenant() -> str:
    """Return the active tenant_id, creating the row if needed."""
    tenant_id = os.getenv("SOLOMON_TENANT_ID", "default")
    business_name = os.getenv("SOLOMON_BUSINESS_NAME", "My Business")
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "INSERT INTO tenants (tenant_id, business_name) "
                    "VALUES (?, ?) ON CONFLICT (tenant_id) DO NOTHING",
                    (tenant_id, business_name),
                )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("ensure tenant failed: %s", e)
    return tenant_id


def _session_id_for(domain: str, ordinal: int) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"onboarding-{ordinal:02d}-{domain}-{stamp}"


def open_or_resume(
    session_key: str,
    *,
    tenant_id: Optional[str] = None,
) -> Tuple[ActiveInterview, bool]:
    """Open (or resume) a session row for the given session_key.

    Returns ``(active_interview, resumed)``. ``resumed=True`` iff we
    attached to a row that was already ``status='open'`` for the same
    (tenant, domain).

    Raises ``ValueError`` if the session_key is not one of the seven
    valid keys.
    """
    if session_key not in SESSION_KEY_TO_DOMAIN:
        valid = ", ".join(sorted(SESSION_KEY_TO_DOMAIN.keys()))
        raise ValueError(
            f"Unknown session key '{session_key}'. Valid: {valid}."
        )
    domain, ordinal = SESSION_KEY_TO_DOMAIN[session_key]
    skill = DOMAIN_TO_SKILL[domain]
    tenant = tenant_id or ensure_tenant()

    # 1. Look for an existing open session for this (tenant, domain).
    resumed_id: Optional[str] = None
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT session_id FROM sessions "
                "WHERE tenant_id=? AND domain=? AND status='open' "
                "ORDER BY started_at DESC LIMIT 1",
                (tenant, domain),
            )
            row = cur.fetchone()
            if row is not None:
                resumed_id = row[0] if not hasattr(row, "keys") else row["session_id"]
        if resumed_id is None:
            new_id = _session_id_for(domain, ordinal)
            # If today's stamp collides with an existing complete row, append a -N suffix.
            with cursor(conn) as cur:
                execute(cur, "SELECT session_id FROM sessions WHERE session_id=?", (new_id,))
                if cur.fetchone() is not None:
                    new_id = f"{new_id}-{uuid.uuid4().hex[:6]}"
                execute(
                    cur,
                    "INSERT INTO sessions "
                    "(session_id, tenant_id, domain, mode, status, started_at, items_captured, turn_count) "
                    "VALUES (?, ?, ?, 'onboarding', 'open', datetime('now'), 0, 0)",
                    (new_id, tenant, domain),
                )
            conn.commit()
            resumed_id = new_id

    iv = ActiveInterview(
        db_session_id=resumed_id,
        domain=domain,
        ordinal=ordinal,
        skill_name=skill,
        tenant_id=tenant,
    )
    return iv, (resumed_id != _session_id_for(domain, ordinal))


def abandon(db_session_id: str) -> None:
    """Mark the session row status='abandoned'."""
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "UPDATE sessions SET status='abandoned', completed_at=datetime('now') "
                    "WHERE session_id=?",
                    (db_session_id,),
                )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("abandon session %s failed: %s", db_session_id, e)


def complete(db_session_id: str) -> None:
    """Mark the session row status='complete'."""
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "UPDATE sessions SET status='complete', completed_at=datetime('now') "
                    "WHERE session_id=?",
                    (db_session_id,),
                )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("complete session %s failed: %s", db_session_id, e)


def increment_turn_count(db_session_id: str) -> None:
    """Bump turn_count on the session row by 1."""
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "UPDATE sessions SET turn_count = turn_count + 1 WHERE session_id=?",
                    (db_session_id,),
                )
            conn.commit()
    except Exception:  # noqa: BLE001
        pass


def rebuild_registry_from_db(reg: OnboardingSessionRegistry) -> None:
    """On conductor startup, repopulate the registry from open sessions.

    Note: we don't store ``hermes_session_id`` on the sessions row today,
    so this is a best-effort: any open onboarding row is left open in the
    DB, and the owner can ``/onboard <session_key>`` again to re-attach
    to it in the current Hermes session.
    """
    # Intentionally a no-op for now. The owner re-runs /onboard after
    # a Hermes restart; open_or_resume will resume the existing row.
    # This stub exists so we can wire durable mapping later.
    pass

"""/private mode — the kill switch.

When active, Solomon does not score, classify, audit, log, embed, or
predict anything for that session until the user toggles it off or the
session ends.

What private mode still does:
  - The non-negotiable check (the safety guardrail)
  - Logs one row to private_sessions with start/end/turn_count but
    NEVER the content

What ends a private session:
  - /private off
  - /endprivate
  - Hermes session_end hook

User contract: private means private. No recovery, no soft prompts, no
auto-detection. If you forget you're in private mode and have a real
business conversation, that data is gone. The cost of an occasional
forgotten conversation is small. The cost of users not trusting the
kill switch is large.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("solomon.private")


@dataclass
class PrivateSessionState:
    active: bool = False
    started_at: Optional[datetime] = None
    turn_count: int = 0
    db_row_id: Optional[int] = None  # private_sessions.private_id


class PrivateMode:
    """Tracks which Hermes sessions are currently in private mode."""

    def __init__(self, adapter) -> None:  # noqa: ANN001
        self.adapter = adapter
        self._state: Dict[str, PrivateSessionState] = {}

    def register_command(self) -> None:
        """Wire up /private and /endprivate as slash commands."""
        self.adapter.register_command(
            name="private",
            aliases=["priv"],
            description="Toggle private mode. In private mode, nothing about this conversation is logged, classified, audited, or remembered. Non-negotiable guardrails still run.",
            handler=self._handle_private_command,
        )
        self.adapter.register_command(
            name="endprivate",
            aliases=[],
            description="End private mode and resume normal Solomon learning.",
            handler=self._handle_endprivate_command,
        )

    def _handle_private_command(self, args: str = "", session_id: str = "", **kwargs: Any) -> str:
        """Handler for /private. With no args -> toggle on. With 'off' -> toggle off."""
        arg = (args or "").strip().lower()
        if arg in ("off", "end", "stop", "no"):
            return self._toggle_off(session_id)
        return self._toggle_on(session_id)

    def _handle_endprivate_command(self, args: str = "", session_id: str = "", **kwargs: Any) -> str:
        return self._toggle_off(session_id)

    def _toggle_on(self, session_id: str) -> str:
        state = self._state.setdefault(session_id, PrivateSessionState())
        if state.active:
            return "🔒 Already in private mode. Nothing from this conversation will be logged."
        state.active = True
        state.started_at = datetime.now(timezone.utc)
        state.turn_count = 0
        try:
            state.db_row_id = self._log_private_start(session_id, state.started_at)
        except Exception as e:  # noqa: BLE001
            # If the DB write fails, we still honor private mode in memory.
            # Better to over-protect than to under-protect.
            logger.warning("Failed to log private session start: %s", e)
        return (
            "🔒 Private mode on. Nothing about this conversation will be "
            "scored, classified, audited, embedded, or stored in the "
            "decision log. Non-negotiable guardrails still run. "
            "Use `/private off` or `/endprivate` to resume normal mode."
        )

    def _toggle_off(self, session_id: str) -> str:
        state = self._state.get(session_id)
        if state is None or not state.active:
            return "Already in normal mode."
        state.active = False
        ended_at = datetime.now(timezone.utc)
        try:
            self._log_private_end(state.db_row_id, ended_at, state.turn_count)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to log private session end: %s", e)
        # Keep the state object around so post_llm_call sees `.active=False`
        # cleanly; it gets dropped at session_end.
        return (
            f"🔓 Private mode off. {state.turn_count} turns were not logged. "
            "Solomon is learning from this conversation again."
        )

    # -- internal API used by conductor -------------------------------------

    def is_active(self, session_id: str) -> bool:
        state = self._state.get(session_id)
        return bool(state and state.active)

    def record_private_turn(self, session_id: str) -> None:
        state = self._state.get(session_id)
        if state and state.active:
            state.turn_count += 1

    def on_session_start(self, session_id: str) -> None:
        # Sessions always start NOT in private mode. The user must
        # explicitly toggle it on. This is the safer default.
        self._state[session_id] = PrivateSessionState(active=False)

    def on_session_end(self, session_id: str) -> None:
        state = self._state.pop(session_id, None)
        if state and state.active:
            try:
                self._log_private_end(state.db_row_id, datetime.now(timezone.utc), state.turn_count)
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to finalize private session at session_end: %s", e)

    # -- db helpers ---------------------------------------------------------

    def _log_private_start(self, session_id: str, started_at: datetime) -> Optional[int]:
        from ..storage.pool import get_pool
        from ..storage.decisions import get_or_create_tenant_id
        tenant_id = get_or_create_tenant_id()
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO private_sessions (tenant_id, session_id, started_at) "
                    "VALUES (%s, %s, %s) RETURNING private_id;",
                    (tenant_id, session_id, started_at),
                )
                row = cur.fetchone()
            conn.commit()
            return int(row[0]) if row else None

    def _log_private_end(self, db_row_id: Optional[int], ended_at: datetime, turn_count: int) -> None:
        if db_row_id is None:
            return
        from ..storage.pool import get_pool
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE private_sessions SET ended_at=%s, turn_count=%s WHERE private_id=%s;",
                    (ended_at, turn_count, db_row_id),
                )
            conn.commit()

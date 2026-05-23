"""Slash commands for skill-driven onboarding.

``/onboard <session_key>`` — open or resume an onboarding interview for
the current Hermes session. The slash command opens the database row,
registers the session in the in-memory registry, and prints the kickoff
message. The actual interview is driven by the LLM following the loaded
skill on subsequent turns (the conductor injects the skill body + state
into messages on every turn while the session is active).

``/endinterview`` — abandon the active interview for this Hermes session.

The pattern mirrors ``solomon/private/mode.py``: register two commands,
manage in-memory state per session_id, plain text responses.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict

from . import session as session_mod
from .session import (
    DOMAIN_TO_SKILL,
    SESSION_KEY_TO_DOMAIN,
    ActiveInterview,
    OnboardingSessionRegistry,
)

logger = logging.getLogger("solomon.onboarding_v2.commands")

SKILLS_ROOT = Path(
    os.path.expanduser(os.getenv("HERMES_HOME", "~/.hermes"))
) / "skills" / "solomon-onboarding"


def _skill_path(skill_name: str) -> Path:
    """Locate the SKILL.md file for a given skill name."""
    direct = SKILLS_ROOT / skill_name / "SKILL.md"
    if direct.exists():
        return direct
    # interview-engine and friends live under skills/solomon-onboarding/interview/
    nested = SKILLS_ROOT / "interview" / skill_name / "SKILL.md"
    if nested.exists():
        return nested
    return direct  # caller will deal with missing-file error


class OnboardingCommands:
    """Registers ``/onboard`` and ``/endinterview`` slash commands."""

    def __init__(self, adapter, registry: OnboardingSessionRegistry) -> None:  # noqa: ANN001
        self.adapter = adapter
        self.registry = registry

    def register_command(self) -> None:
        self.adapter.register_command(
            name="onboard",
            aliases=["interview"],
            description=(
                "Start or resume a Solomon onboarding interview. "
                "Usage: /onboard session_0 (industry), session_1 (belief system), "
                "session_2 (why), session_3 (principles), session_4 (ideal outcomes), "
                "session_5 (non-negotiables), session_6 (scopes)."
            ),
            handler=self._handle_onboard,
        )
        self.adapter.register_command(
            name="endinterview",
            aliases=["endonboard", "abandon"],
            description="End the active Solomon onboarding interview without completing it. Captures are preserved.",
            handler=self._handle_endinterview,
        )
        self.adapter.register_command(
            name="onboarding",
            aliases=["interviews"],
            description="Show the status of all Solomon onboarding sessions.",
            handler=self._handle_status,
        )

    # -- handlers -----------------------------------------------------------

    def _handle_onboard(self, args: str = "", session_id: str = "", **kwargs: Any) -> str:
        key = (args or "").strip().lower() or "session_0"
        if key not in SESSION_KEY_TO_DOMAIN:
            valid = ", ".join(sorted(SESSION_KEY_TO_DOMAIN.keys()))
            return f"Unknown session key '{key}'. Valid: {valid}."

        # Refuse to overwrite an active session.
        existing = self.registry.get(session_id)
        if existing is not None:
            return (
                f"You're already in onboarding {existing.domain} "
                f"(session id {existing.db_session_id}). "
                f"Type /endinterview to abandon it, or finish answering first."
            )

        try:
            iv, resumed = session_mod.open_or_resume(key)
        except ValueError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            logger.exception("open_or_resume failed for %s", key)
            return f"Could not open onboarding session: {e}"

        self.registry.register(session_id, iv)

        domain_label = iv.domain.replace("_", " ")
        resume_note = " (resumed an existing session)" if resumed else ""

        # Tell Hermes/the LLM which skill to load on the next turn.
        # The conductor will inject the skill body itself; this message
        # is what the user sees in chat.
        return (
            f"Starting Solomon onboarding: {domain_label}{resume_note}.\n\n"
            f"I'll follow the {iv.skill_name} skill. The first question is "
            f"coming up. To stop early, type /endinterview — captures so far "
            f"will be preserved."
        )

    def _handle_endinterview(self, args: str = "", session_id: str = "", **kwargs: Any) -> str:
        iv = self.registry.clear(session_id)
        if iv is None:
            return "No active onboarding interview in this session."
        try:
            session_mod.abandon(iv.db_session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("abandon failed: %s", e)
        return (
            f"Ended onboarding {iv.domain} (session id {iv.db_session_id}). "
            f"Your captures so far are preserved."
        )

    def _handle_status(self, args: str = "", session_id: str = "", **kwargs: Any) -> str:
        try:
            tenant = session_mod.ensure_tenant()
            from ..storage.pool import cursor, execute, get_conn
            sessions: list[tuple] = []
            with get_conn() as conn:
                with cursor(conn) as cur:
                    execute(
                        cur,
                        "SELECT session_id, domain, status, items_captured, turn_count "
                        "FROM sessions WHERE tenant_id=? AND mode='onboarding' "
                        "ORDER BY started_at ASC",
                        (tenant,),
                    )
                    for r in cur.fetchall():
                        if hasattr(r, "keys"):
                            sessions.append((
                                r["session_id"], r["domain"], r["status"],
                                r["items_captured"], r["turn_count"],
                            ))
                        else:
                            sessions.append(tuple(r))
        except Exception as e:  # noqa: BLE001
            return f"Could not read status: {e}"

        if not sessions:
            return (
                "No onboarding sessions yet. Type /onboard session_0 to begin "
                "with industry, or pick another (session_0 through session_6)."
            )

        # One line per session.
        lines = ["Solomon onboarding status:"]
        # Group by domain — show latest status per ordinal.
        domain_status: Dict[str, tuple] = {}
        for sid, dom, status, items, turns in sessions:
            domain_status[dom] = (sid, status, items, turns)
        ordered = sorted(
            SESSION_KEY_TO_DOMAIN.items(), key=lambda kv: kv[1][1]
        )
        for key, (dom, ordinal) in ordered:
            row = domain_status.get(dom)
            if row is None:
                lines.append(f"  {ordinal}. {dom}: not started")
            else:
                sid, status, items, turns = row
                lines.append(
                    f"  {ordinal}. {dom}: {status} "
                    f"({items} captures, {turns} turns) — {sid}"
                )
        active = self.registry.get(session_id)
        if active is not None:
            lines.append(f"\nActive in this chat: {active.domain} ({active.db_session_id}).")
        return "\n".join(lines)

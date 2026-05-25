"""Slash command handlers.

Eight commands total:
  /onboard       — continue the next foundation interview
  /mentor        — weekly review with active probing
  /status        — plain printout (no LLM)
  /private       — toggle private mode for the conversation
  /reflect       — run daily.py now
  /ingest        — process the inbox now
  /solomon-off   — global suspend
  /solomon-on    — resume

Most handlers return a structured response. The LLM-driven commands
(/onboard, /mentor) load the interview skill and let Hermes do the
talking. Pure-text commands (/status, /private, etc.) return strings
directly.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from . import logs, profile

SKILL_DIR = Path(__file__).parent / "skills"


# ---------------------------------------------------------------------------
# Helpers used by /onboard and /mentor
# ---------------------------------------------------------------------------


def _load_interview_skill_body() -> str:
    text = (SKILL_DIR / "solomon-interview.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---\n", 3)
        if end != -1:
            text = text[end + 5 :]
    return text.strip()


def _next_unfilled_session() -> Optional[int]:
    """Return the lowest session number (0-6) that isn't yet filled."""
    data = yaml.safe_load((profile.home() / "profile.yaml").read_text())
    for n, section in profile.SESSION_SECTION.items():
        if not data.get(section, {}).get("filled"):
            return n
    return None


def _required_fields_for(session_n: int) -> tuple[str, ...]:
    return profile.SESSION_REQUIRED_FIELDS[session_n]


# ---------------------------------------------------------------------------
# /onboard
# ---------------------------------------------------------------------------


def cmd_onboard(args: dict, session: Any = None) -> dict:
    """Open an onboarding conversation for the next unfilled session."""
    profile.init_solomon_home()
    n = _next_unfilled_session()
    if n is None:
        return {
            "ok": True,
            "system_prompt": None,
            "message": (
                "All seven foundation sessions are complete. Your profile "
                "is filled. Use /mentor to deepen specific areas."
            ),
        }
    fields = _required_fields_for(n)
    skill_body = _load_interview_skill_body()
    addendum = (
        f"\n\n# Active mode\n\nMODE: onboarding\n"
        f"SESSION: {n} ({profile.SESSION_NAMES[n]})\n"
        f"REQUIRED FIELDS: {', '.join(fields)}\n"
        f"SESSION SCHEMA: see profile.yaml.{profile.SESSION_SECTION[n]} section. "
        f"Each field maps to a key in the dict you pass to mark_session_complete."
    )
    if session is not None:
        try:
            session.solomon_skill_overridden = True
        except Exception:  # noqa: BLE001
            pass
    logs.log("skill_loaded", skill="solomon-interview",
             context={"mode": "onboarding", "session_n": n})
    return {
        "ok": True,
        "system_prompt": skill_body + addendum,
        "message": (
            f"Starting onboarding session {n} — {profile.SESSION_NAMES[n]}. "
            "I'll ask one question at a time. Take your time. "
            "Stop any time; the next /onboard picks up where we left off."
        ),
        "session_n": n,
    }


# ---------------------------------------------------------------------------
# /mentor
# ---------------------------------------------------------------------------


def cmd_mentor(args: dict, session: Any = None) -> dict:
    profile.init_solomon_home()
    review_pending = profile.read_queue("review", "pending", limit=100)
    actions_ignored = [
        a for a in profile.read_queue("actions", "pending", limit=100)
        if a.get("nudge_count", 0) >= 2
    ]
    actions_stale = profile.read_queue("actions", "stale", limit=100)

    skill_body = _load_interview_skill_body()
    addendum = (
        f"\n\n# Active mode\n\nMODE: mentoring\n"
        f"REVIEW QUEUE PENDING: {len(review_pending)}\n"
        f"ACTIONS IGNORED (nudge_count >= 2): {len(actions_ignored)}\n"
        f"ACTIONS STALE: {len(actions_stale)}\n\n"
        "Walk stale + ignored actions first, then the review queue, "
        "then ask one hypothetical, then probe one gap. Use "
        "apply_queue_decision to act on each item."
    )
    if session is not None:
        try:
            session.solomon_skill_overridden = True
        except Exception:  # noqa: BLE001
            pass
    logs.log("skill_loaded", skill="solomon-interview",
             context={"mode": "mentoring",
                       "review_pending": len(review_pending),
                       "ignored_actions": len(actions_ignored),
                       "stale_actions": len(actions_stale)})

    total = len(review_pending) + len(actions_ignored) + len(actions_stale)
    if total == 0:
        opener = (
            "Nothing in your queue right now. Want me to ask a hypothetical "
            "or probe one of the thin sections in your profile?"
        )
    else:
        bits = []
        if actions_stale:
            bits.append(f"{len(actions_stale)} stale action(s)")
        if actions_ignored:
            bits.append(f"{len(actions_ignored)} pending action(s) you've been ignoring")
        if review_pending:
            bits.append(f"{len(review_pending)} review item(s)")
        opener = (
            f"I have {' + '.join(bits)} to go through. Want to start with the "
            "actions or the captures?"
        )
    return {
        "ok": True,
        "system_prompt": skill_body + addendum,
        "message": opener,
        "context": {
            "review_pending": review_pending,
            "actions_ignored": actions_ignored,
            "actions_stale": actions_stale,
        },
    }


# ---------------------------------------------------------------------------
# /status — pure text, no LLM
# ---------------------------------------------------------------------------


def cmd_status(args: dict, session: Any = None) -> dict:
    profile.init_solomon_home()
    data = yaml.safe_load((profile.home() / "profile.yaml").read_text())

    sessions_lines = []
    filled_count = 0
    for n in range(7):
        section = profile.SESSION_SECTION[n]
        sect = data.get(section, {})
        name = profile.SESSION_NAMES[n]
        if sect.get("filled"):
            filled_count += 1
            filled_at = sect.get("filled_at", "")
            short_date = filled_at.split("T")[0] if filled_at else ""
            sessions_lines.append(f"  ✓ {n}  {name:<24}  ({short_date})")
        else:
            sessions_lines.append(f"  ☐ {n}  {name}")

    review_pending = profile.read_queue("review", "pending", limit=10_000)
    actions = profile.read_queue("actions", "all", limit=10_000)
    actions_pending = [a for a in actions if a.get("status") == "pending"]
    actions_stale = [a for a in actions if a.get("status") == "stale"]

    inbox = profile.home() / "inbox"
    inbox_files = [p.name for p in inbox.iterdir() if p.is_file()] if inbox.exists() else []

    last_activity = _last_activity_ts()

    lines = ["Solomon — status\n",
             f"Foundation sessions: {filled_count} of 7 complete"]
    lines.extend(sessions_lines)
    lines.append("")

    # Actions
    if actions_pending or actions_stale:
        by_urg = {"high": 0, "medium": 0, "low": 0}
        for a in actions_pending:
            by_urg[a.get("urgency", "low")] = by_urg.get(a.get("urgency", "low"), 0) + 1
        lines.append(
            f"Pending actions: {len(actions_pending)} pending "
            f"({by_urg['high']} high, {by_urg['medium']} medium, {by_urg['low']} low)"
            + (f", {len(actions_stale)} stale" if actions_stale else "")
        )
        # Show up to 3 highest-urgency pending items.
        prioritized = sorted(actions_pending,
                              key=lambda a: {"high": 0, "medium": 1, "low": 2}.get(a.get("urgency", "low"), 3))
        for a in prioritized[:3]:
            ts = a.get("ts", "")
            short_ts = ts.split("T")[0] if ts else "?"
            urg = a.get("urgency", "low").upper()
            summary = (a.get("source_summary") or "?")[:60]
            lines.append(f"  - {urg:<4} {summary}  (proposed {short_ts}, nudged {a.get('nudge_count', 0)}x)")
        if len(actions_pending) > 3:
            lines.append(f"  ... and {len(actions_pending) - 3} more — type /mentor to walk through them")
        if actions_stale:
            lines.append("Run /mentor to address stale items so Solomon can resume nudging.")
        lines.append("")

    lines.append(f"Review queue: {len(review_pending)} pending")
    lines.append(f"Documents in inbox: {len(inbox_files)}")
    if last_activity:
        lines.append(f"Last activity: {last_activity}")
    lines.append("")
    if filled_count < 7:
        lines.append("Type /onboard to continue. Type /mentor when you have items to review.")
    else:
        lines.append("Type /mentor when you're ready to walk through pending items.")

    return {"ok": True, "system_prompt": None, "message": "\n".join(lines)}


def _last_activity_ts() -> Optional[str]:
    log_file = logs.log_path()
    if not log_file.exists():
        return None
    last = ""
    import json
    try:
        for line in log_file.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("event") == "turn_end":
                last = entry.get("ts", "")
    except Exception:  # noqa: BLE001
        return None
    if not last:
        return None
    # Render as YYYY-MM-DD HH:MM.
    try:
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return last


# ---------------------------------------------------------------------------
# /private — toggle
# ---------------------------------------------------------------------------


def cmd_private(args: dict, session: Any = None) -> dict:
    current = bool(getattr(session, "private", False)) if session else False
    new = not current
    if session is not None:
        try:
            session.private = new
        except Exception:  # noqa: BLE001
            pass
    if new:
        logs.log("private_activated", session_id=getattr(session, "id", None))
        msg = ("Private mode is on for this conversation. Nothing said from "
               "here on will be logged, learned from, or added to the review "
               "queue. This conversation will not appear in tomorrow's "
               "reflection. Type /private again to turn it back off.")
    else:
        logs.log("private_deactivated", session_id=getattr(session, "id", None))
        msg = ("Private mode is off. From this point forward in this "
               "conversation, Solomon is loaded and learning resumes. "
               "The turns that happened in private mode remain unlogged.")
    return {"ok": True, "system_prompt": None, "message": msg}


# ---------------------------------------------------------------------------
# /reflect — run daily.py now
# ---------------------------------------------------------------------------


def cmd_reflect(args: dict, session: Any = None) -> dict:
    from . import daily
    result = daily.run(adapter=getattr(session, "adapter", None))
    msg = (
        f"Reflection complete.\n"
        f"  Conversation batches processed: {result.get('batches', 0)}\n"
        f"  Documents ingested: {result.get('files', 0)}\n"
        f"  Proposals added: {result.get('proposals', 0)}\n"
        f"  Nudges sent: {result.get('nudges_sent', 0)}\n"
        f"  Actions marked stale: {result.get('actions_stale', 0)}"
    )
    return {"ok": True, "system_prompt": None, "message": msg}


# ---------------------------------------------------------------------------
# /ingest — process inbox now
# ---------------------------------------------------------------------------


def cmd_ingest(args: dict, session: Any = None) -> dict:
    from . import ingest
    adapter = getattr(session, "adapter", None)
    inbox = profile.home() / "inbox"
    if not inbox.exists() or not any(inbox.iterdir()):
        return {
            "ok": True,
            "system_prompt": None,
            "message": ("Inbox is empty. Drop files into ~/.hermes/solomon/inbox/ "
                         "first, then run /ingest again."),
        }
    summary = ingest.process_all(adapter=adapter)
    lines = ["Ingest complete."]
    lines.append(f"  Files processed: {summary['ok']}")
    if summary['failed']:
        lines.append(f"  Files failed: {summary['failed']} (see archive/failed/)")
    lines.append(f"  Proposals added: {summary['proposals']}")
    return {"ok": True, "system_prompt": None, "message": "\n".join(lines)}


# ---------------------------------------------------------------------------
# /solomon-off and /solomon-on
# ---------------------------------------------------------------------------


def cmd_solomon_off(args: dict, session: Any = None) -> dict:
    profile.init_solomon_home()
    (profile.home() / ".solomon_off").touch()
    logs.log("solomon_suspended")
    return {
        "ok": True,
        "system_prompt": None,
        "message": (
            "Solomon is globally suspended. Hermes is now running without the "
            "Solomon role until you type /solomon-on. Pending learning "
            "continues to be captured in the review queue but no system-prompt "
            "injection happens on Hermes turns."
        ),
    }


def cmd_solomon_on(args: dict, session: Any = None) -> dict:
    profile.init_solomon_home()
    sentinel = profile.home() / ".solomon_off"
    if sentinel.exists():
        sentinel.unlink()
    logs.log("solomon_resumed")
    return {
        "ok": True,
        "system_prompt": None,
        "message": (
            "Solomon is active again. The next Hermes turn will be loaded "
            "with the Solomon role."
        ),
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

COMMANDS = {
    "onboard": (cmd_onboard, "Start or continue the foundation interview."),
    "mentor": (cmd_mentor, "Walk through pending items and probe gaps."),
    "status": (cmd_status, "Show progress: foundation sessions, pending actions, inbox."),
    "private": (cmd_private, "Toggle private mode for this conversation."),
    "reflect": (cmd_reflect, "Run nightly reflection now."),
    "ingest": (cmd_ingest, "Process documents in ~/.hermes/solomon/inbox/ now."),
    "solomon-off": (cmd_solomon_off, "Globally suspend Solomon."),
    "solomon-on": (cmd_solomon_on, "Resume Solomon."),
}


def register_all(adapter) -> None:  # noqa: ANN001
    for name, (fn, desc) in COMMANDS.items():
        adapter.register_command(name=name, description=desc, handler=_make_handler(fn))


def _make_handler(fn):
    def handler(args: dict, session=None) -> Any:
        try:
            return fn(args, session)
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where=f"slash.{fn.__name__}")
            return {"ok": False, "message": f"Sorry — that command hit an error. Run `solomon logs --errors --today` for details."}
    handler.__name__ = fn.__name__
    return handler

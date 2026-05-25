"""Slash command handlers — real Hermes signature.

Hermes invokes each handler with a raw args string and expects a `str | None`
response. The response IS the text shown to the owner. The LLM is NOT
involved in producing that response.

For commands that need to start an LLM-driven session (/onboard, /mentor,
/private, /endprivate), the handler writes a "pending intent" via
session_state.push_pending_intent(). The next pre_llm_call claims it and
sets the active mode for the owner's session — see hooks.pre_llm_call.

Eight commands registered:
  /onboard       /mentor       /status       /private
  /endprivate    /reflect      /ingest       /solomon-off  /solomon-on

(That is nine. `/endprivate` is the symmetrical undo of `/private`. The
plan called them out as eight "primary" commands plus the on/off pair;
implementation lists them all.)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from . import logs, profile, session_state


# ---------------------------------------------------------------------------
# Helpers used by /onboard and /mentor
# ---------------------------------------------------------------------------


def _next_unfilled_session() -> Optional[int]:
    """Return the lowest session number (0-6) that isn't yet filled."""
    data = yaml.safe_load((profile.home() / "profile.yaml").read_text())
    for n, section in profile.SESSION_SECTION.items():
        if not (data.get(section, {}) or {}).get("filled"):
            return n
    return None


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
    try:
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return last


# ---------------------------------------------------------------------------
# /onboard
# ---------------------------------------------------------------------------


def cmd_onboard(raw_args: str) -> str:
    """Continue the foundation interview. Args ignored (we always pick the next unfilled session)."""
    profile.init_solomon_home()
    n = _next_unfilled_session()
    if n is None:
        return ("All seven foundation sessions are complete. Your profile is "
                "filled. Use /mentor to deepen specific areas.")
    session_state.push_pending_intent("onboarding", session_n=n)
    name = profile.SESSION_NAMES[n]
    return (
        f"Starting session {n} — {name}. I'll ask one question at a time. "
        "Take your time. Stop any time; the next /onboard picks up where we "
        "left off.\n\n"
        "Reply to this with whatever you want to say first."
    )


# ---------------------------------------------------------------------------
# /mentor
# ---------------------------------------------------------------------------


def cmd_mentor(raw_args: str) -> str:
    profile.init_solomon_home()
    review_pending = profile.read_queue("review", "pending", limit=10_000)
    actions_pending = profile.read_queue("actions", "pending", limit=10_000)
    actions_ignored = [a for a in actions_pending if a.get("nudge_count", 0) >= 2]
    actions_stale = profile.read_queue("actions", "stale", limit=10_000)

    session_state.push_pending_intent(
        "mentoring",
        queue_count=len(review_pending),
        action_count=len(actions_ignored) + len(actions_stale),
    )

    total = len(review_pending) + len(actions_ignored) + len(actions_stale)
    if total == 0:
        return ("Nothing in your queue right now. Reply to this and we can "
                "talk through a hypothetical or probe a thin section of your "
                "profile.")
    bits = []
    if actions_stale:
        bits.append(f"{len(actions_stale)} stale action(s)")
    if actions_ignored:
        bits.append(f"{len(actions_ignored)} pending action(s) you've been ignoring")
    if review_pending:
        bits.append(f"{len(review_pending)} review item(s)")
    return (f"I have {' + '.join(bits)} to walk through. Want to start with "
            "the actions or the captures?")


# ---------------------------------------------------------------------------
# /status — no LLM, no pending intent, just text
# ---------------------------------------------------------------------------


def cmd_status(raw_args: str) -> str:
    profile.init_solomon_home()
    data = yaml.safe_load((profile.home() / "profile.yaml").read_text())

    sessions_lines = []
    filled_count = 0
    for n in range(7):
        section = profile.SESSION_SECTION[n]
        sect = data.get(section, {}) or {}
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

    if actions_pending or actions_stale:
        by_urg = {"high": 0, "medium": 0, "low": 0}
        for a in actions_pending:
            by_urg[a.get("urgency", "low")] = by_urg.get(a.get("urgency", "low"), 0) + 1
        line = (
            f"Pending actions: {len(actions_pending)} pending "
            f"({by_urg['high']} high, {by_urg['medium']} medium, {by_urg['low']} low)"
        )
        if actions_stale:
            line += f", {len(actions_stale)} stale"
        lines.append(line)
        prioritized = sorted(
            actions_pending,
            key=lambda a: {"high": 0, "medium": 1, "low": 2}.get(a.get("urgency", "low"), 3),
        )
        for a in prioritized[:3]:
            ts = a.get("ts", "")
            short_ts = ts.split("T")[0] if ts else "?"
            urg = a.get("urgency", "low").upper()
            summary = (a.get("source_summary") or "?")[:60]
            lines.append(f"  - {urg:<6} {summary}  (proposed {short_ts}, nudged {a.get('nudge_count', 0)}x)")
        if len(actions_pending) > 3:
            lines.append(f"  ... and {len(actions_pending) - 3} more — type /mentor to walk through them")
        if actions_stale:
            lines.append("Run /mentor to address stale items.")
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

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /private and /endprivate — pending intent for next turn
# ---------------------------------------------------------------------------


def cmd_private(raw_args: str) -> str:
    session_state.push_pending_intent("private_on")
    return ("Private mode will turn on for this conversation starting with "
            "your next message. Nothing said from here on will be logged, "
            "learned from, or added to the review queue. Type /endprivate to "
            "turn it back off.")


def cmd_endprivate(raw_args: str) -> str:
    session_state.push_pending_intent("private_off")
    return ("Private mode will turn off starting with your next message. "
            "Solomon resumes loading the role and learning. Anything said "
            "during private mode remains unlogged.")


# ---------------------------------------------------------------------------
# /reflect — run daily now (manual override)
# ---------------------------------------------------------------------------


def cmd_reflect(raw_args: str) -> str:
    from . import daily
    result = daily.run_now()
    return (
        "Reflection complete.\n"
        f"  Conversation batches processed: {result.get('batches', 0)}\n"
        f"  Documents ingested: {result.get('files', 0)}\n"
        f"  Proposals added: {result.get('proposals', 0)}\n"
        f"  Nudges sent: {result.get('nudges_sent', 0)}\n"
        f"  Actions marked stale: {result.get('actions_stale', 0)}"
    )


# ---------------------------------------------------------------------------
# /ingest — process inbox now
# ---------------------------------------------------------------------------


def cmd_ingest(raw_args: str) -> str:
    """Process the inbox now.

    Under the v3 design, ingestion is one of four steps the daily-reflection
    cron does. `/ingest` is a manual override that fires the same cron right
    now — same prompt, same tools, just no waiting until 02:00.
    """
    profile.init_solomon_home()
    inbox = profile.home() / "inbox"
    if not inbox.exists() or not any(inbox.iterdir()):
        return ("Inbox is empty. Drop files into ~/.hermes/solomon/inbox/ "
                "first, then run /ingest again.")
    from . import daily
    result = daily.run_now()
    if not result.get("ok"):
        return ("Couldn't run ingestion right now. Cause: "
                f"{result.get('reason') or result.get('error') or 'unknown'}.")
    final = (result.get("final_response") or "").strip()
    if final == "[SILENT]":
        return "Ingest ran but nothing new to capture."
    return f"Ingest complete.\n\n{final}" if final else "Ingest complete."


# ---------------------------------------------------------------------------
# /solomon-off and /solomon-on
# ---------------------------------------------------------------------------


def cmd_solomon_off(raw_args: str) -> str:
    profile.init_solomon_home()
    (profile.home() / ".solomon_off").touch()
    logs.log("solomon_suspended")
    return ("Solomon is globally suspended. Hermes is now running without "
            "the Solomon role until you type /solomon-on. Pending learning "
            "continues to be captured in the review queue but no system-prompt "
            "injection happens on Hermes turns.")


def cmd_solomon_on(raw_args: str) -> str:
    profile.init_solomon_home()
    sentinel = profile.home() / ".solomon_off"
    if sentinel.exists():
        sentinel.unlink()
    logs.log("solomon_resumed")
    return ("Solomon is active again. The next Hermes turn will be loaded "
            "with the Solomon role.")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

COMMANDS = {
    "onboard": (cmd_onboard, "Start or continue the foundation interview."),
    "mentor": (cmd_mentor, "Walk through pending items and probe gaps."),
    # `/status` is a Hermes built-in command; we register under `/solomon-status`
    # to avoid the collision (Hermes drops conflicting plugin commands).
    "solomon-status": (cmd_status, "Show Solomon progress: foundation sessions, pending actions, inbox."),
    "private": (cmd_private, "Turn private mode on for this conversation."),
    "endprivate": (cmd_endprivate, "Turn private mode off."),
    "reflect": (cmd_reflect, "Run nightly reflection now."),
    "ingest": (cmd_ingest, "Process documents in ~/.hermes/solomon/inbox/ now."),
    "solomon-off": (cmd_solomon_off, "Globally suspend Solomon."),
    "solomon-on": (cmd_solomon_on, "Resume Solomon."),
}


def register_all(adapter_obj) -> None:  # noqa: ANN001
    for name, (fn, desc) in COMMANDS.items():
        adapter_obj.register_command(
            name=name, description=desc, handler=_wrap_handler(fn),
        )


def _wrap_handler(fn):
    """Wrap a handler so exceptions become a user-facing error string.

    Hermes catches plugin exceptions, but we'd rather show the owner a
    plain message + a doctor hint than have the slash command just go quiet.
    """
    def handler(raw_args: str) -> Optional[str]:
        try:
            return fn(raw_args)
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where=f"slash.{fn.__name__}")
            return ("Sorry — that command hit an error. Run "
                    "`solomon logs --errors --today` for details.")
    handler.__name__ = fn.__name__
    return handler

"""Weekly LLM-initiated check-in cron.

Picks one or two genuine gaps from the profile + queue + recent activity,
asks the LLM to compose a short message, sends it through the owner's
preferred channel (or queues for retry).
"""

from __future__ import annotations

import fcntl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from . import logs, profile

SKILL_PATH = Path(__file__).parent / "skills" / "solomon-interview.md"


def _lock_path() -> Path:
    return profile.home() / ".checkin.lock"


def _acquire_lock():
    profile.home().mkdir(parents=True, exist_ok=True)
    f = open(_lock_path(), "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def _release_lock(f) -> None:
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()
        _lock_path().unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _skill_body() -> str:
    text = SKILL_PATH.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---\n", 3)
        if end != -1:
            text = text[end + 5:]
    return text.strip()


def _profile_digest() -> str:
    """Lean digest of the profile for the LLM."""
    try:
        data = yaml.safe_load((profile.home() / "profile.yaml").read_text())
    except Exception:  # noqa: BLE001
        return ""
    parts = []
    for sec in ("industry", "belief_system", "why", "principles",
                "ideal_outcomes", "non_negotiables", "scopes"):
        s = data.get(sec, {}) or {}
        status = "filled" if s.get("filled") else "unfilled"
        parts.append(f"{sec}: {status}")
    parts.append(f"preferred_channel: {(data.get('meta') or {}).get('preferred_channel', '')}")
    return "\n".join(parts)


def _queue_digest() -> str:
    review_pending = len(profile.read_queue("review", "pending", limit=10_000))
    contradictions = sum(
        1 for it in profile.read_queue("review", status="pending", limit=10_000)
        if it.get("kind") == "contradiction"
    )
    actions_pending = len(profile.read_queue("actions", "pending", limit=10_000))
    actions_stale = len(profile.read_queue("actions", "stale", limit=10_000))
    return (f"review_pending: {review_pending}\n"
            f"contradictions_unresolved: {contradictions}\n"
            f"actions_pending: {actions_pending}\n"
            f"actions_stale: {actions_stale}")


def _recent_activity(adapter, since_days: int = 7) -> str:
    """Short summary of how active the owner has been."""
    if adapter is None:
        return "no_recent_activity (no adapter)"
    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    try:
        convos = adapter.read_recent_conversations(since)
    except Exception:  # noqa: BLE001
        convos = []
    return f"sessions_last_week: {len(convos)}"


def run(*, adapter: Optional[Any] = None) -> dict:
    """Compose and send the weekly check-in."""
    lock = _acquire_lock()
    if lock is None:
        logs.log("cron_skipped", level="WARN",
                 context={"cron": "checkin", "reason": "lock held"})
        return {"sent": False, "lock_skipped": True}

    logs.log("cron_start", context={"cron": "checkin"})
    summary = {"sent": False, "channel": None, "queued": False}
    try:
        if adapter is None:
            logs.log("checkin_no_adapter", level="WARN")
            return summary

        system = (_skill_body() +
                  "\n\n# Active mode\n\nMODE: checkin\n"
                  "Pick ONE OR TWO genuine gaps from the data below. "
                  "Compose ONE short message (one or two sentences) inviting "
                  "the owner to talk. Just the message text — no preamble, "
                  "no labels.")

        body = (
            f"# Profile digest\n\n{_profile_digest()}\n\n"
            f"# Queue digest\n\n{_queue_digest()}\n\n"
            f"# Recent activity\n\n{_recent_activity(adapter)}"
        )
        try:
            text = adapter.llm_call(system=system,
                                      messages=[{"role": "user", "content": body}],
                                      max_tokens=300)
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where="checkin.llm_call")
            return summary

        text = (text or "").strip()
        if not text:
            logs.log("checkin_empty_response", level="WARN")
            return summary

        # Look up preferred channel.
        try:
            data = yaml.safe_load((profile.home() / "profile.yaml").read_text())
            channel = ((data or {}).get("meta") or {}).get("preferred_channel") or None
        except Exception:  # noqa: BLE001
            channel = None

        sent = False
        try:
            sent = adapter.send_to_owner(text, channel=channel)
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where="checkin.send_to_owner")
            sent = False

        if not sent:
            from . import tools
            tools._queue_pending_message(text, channel)
            summary["queued"] = True
            logs.log("checkin_sent", channel="queued")
        else:
            logs.log("checkin_sent", channel=channel or "(default)")
        summary["sent"] = sent or summary["queued"]
        summary["channel"] = channel or "queued"

        logs.log("cron_end", context={"cron": "checkin", **summary})
    finally:
        _release_lock(lock)
    return summary


def main() -> int:
    run()
    return 0

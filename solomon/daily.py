"""Nightly cron: reflection + ingestion + nudge + pending-message retry.

Runs once a day at 02:00 local. Wraps the work in a single lock file so
two cron firings can't stomp each other. Errors in any single step are
logged and skipped; the next step still runs.
"""

from __future__ import annotations

import fcntl
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import ingest, inbound, logs, profile


def _lock_path() -> Path:
    return profile.home() / ".daily.lock"


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


def reflect_step(*, adapter: Optional[Any] = None,
                  since_hours: int = 24) -> int:
    """Read recent Hermes conversations and feed each session-batch through the
    ingest skill. Returns the number of conversation batches processed.
    """
    if adapter is None:
        return 0
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    convos = adapter.read_recent_conversations(since)
    batches = 0
    for c in convos:
        if c.get("private"):
            continue
        turns = c.get("turns") or []
        # Filter out trivial turns (very short greetings, etc.)
        substantive = [t for t in turns if len((t.get("content") or "").strip()) > 12]
        if not substantive:
            continue
        # Concatenate as a single document for the ingest skill.
        as_text = "\n\n".join(f"{t.get('role', 'user')}: {t.get('content', '')}"
                              for t in substantive)
        # Save to a tmpfile path so ingest.process_file can pick it up.
        tmp_dir = profile.home() / ".reflection"
        tmp_dir.mkdir(exist_ok=True)
        tmp_file = tmp_dir / f"conv_{c.get('session_id', 'unknown')}.txt"
        tmp_file.write_text(as_text, encoding="utf-8")
        try:
            ingest.process_file(tmp_file, adapter=adapter)
        finally:
            # The reflection tmpdir isn't archived. Clean up if the process_file
            # didn't already move it (it moves files in `inbox/` only).
            if tmp_file.exists():
                tmp_file.unlink()
        batches += 1
    # Clean up the reflection dir if empty.
    tmp_dir = profile.home() / ".reflection"
    if tmp_dir.exists() and not any(tmp_dir.iterdir()):
        tmp_dir.rmdir()
    return batches


def run(*, adapter: Optional[Any] = None,
         since_hours: int = 24) -> dict:
    """Run all four daily steps. Returns summary stats."""
    lock = _acquire_lock()
    if lock is None:
        logs.log("cron_skipped", level="WARN", context={"cron": "daily", "reason": "lock held"})
        return {"batches": 0, "files": 0, "proposals": 0,
                "nudges_sent": 0, "actions_stale": 0,
                "pending_messages_sent": 0, "skipped": True}

    logs.log("cron_start", context={"cron": "daily"})
    summary = {"batches": 0, "files": 0, "proposals": 0,
               "nudges_sent": 0, "actions_stale": 0,
               "pending_messages_sent": 0}
    try:
        # Rotate yesterday's log file if needed (best-effort).
        try:
            logs.rotate_if_needed()
            logs.archive_old_logs()
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where="daily.log_rotation")

        # 1. Reflection on yesterday's Hermes turns.
        try:
            summary["batches"] = reflect_step(adapter=adapter,
                                                since_hours=since_hours)
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where="daily.reflect_step")

        # 2. Inbox ingestion.
        try:
            counts_before = len(profile.read_queue("review", "pending", limit=10_000))
            counts = ingest.process_all(adapter=adapter)
            summary["files"] = counts["ok"]
            counts_after = len(profile.read_queue("review", "pending", limit=10_000))
            summary["proposals"] = max(0, counts_after - counts_before)
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where="daily.ingest")

        # 3. Nudge step on pending actions.
        try:
            nudge = inbound.nudge_step(adapter=adapter)
            summary["nudges_sent"] = nudge["sent"]
            summary["actions_stale"] = nudge["stale"]
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where="daily.nudge_step")

        # 4. Retry any pending messages.
        try:
            summary["pending_messages_sent"] = inbound.retry_pending_messages(adapter=adapter)
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where="daily.retry_pending_messages")

        logs.log("cron_end", context={"cron": "daily", **summary})
    finally:
        _release_lock(lock)
    return summary


def run_now() -> dict:
    """Manual override — fire the daily cron once, right now. Used by /reflect.
    Step 6 will replace this and `run` with a thin Hermes-cron registration.
    """
    return run()


def main() -> int:
    """Entry point if invoked as a script."""
    run()
    return 0

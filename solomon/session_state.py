"""Solomon-side runtime session state.

Two small JSON Lines files under ~/.hermes/solomon/, both with atomic writes
and fcntl locks (mirrors profile.py's pattern):

  active_modes.jsonl    — which Hermes sessions are currently in onboarding
                          or mentoring mode. Entries older than 6h are
                          ignored at read time (no cleanup job needed).

  private_sessions.jsonl — Hermes session IDs the owner toggled /private on.
                          The daily reflection filters these out at read time.

These files are intentionally NOT git-tracked (in .gitignore via profile.py).
They are runtime state, not knowledge.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import logs, profile

ACTIVE_MODES_FILE = "active_modes.jsonl"
PRIVATE_SESSIONS_FILE = "private_sessions.jsonl"
PENDING_INTENT_FILE = ".pending_intent.json"
STALE_HOURS = 6
PENDING_INTENT_MAX_AGE_SEC = 60


def _active_modes_path() -> Path:
    return profile.home() / ACTIVE_MODES_FILE


def _private_sessions_path() -> Path:
    return profile.home() / PRIVATE_SESSIONS_FILE


def _pending_intent_path() -> Path:
    return profile.home() / PENDING_INTENT_FILE


# ---------------------------------------------------------------------------
# Active modes (onboarding / mentoring / checkin)
# ---------------------------------------------------------------------------


def set_active_mode(session_id: str, mode: str, **fields) -> None:
    """Record that `session_id` is in `mode`. Overwrites any prior entry for
    the same session_id.

    mode: "onboarding", "mentoring", "checkin", or any future mode string.
    fields: additional context — typically `session_n` for onboarding,
            `queue_count` for mentoring, etc.
    """
    entries = _read_active_modes_raw()
    # Drop any prior entry for this session_id (we keep one per session).
    entries = [e for e in entries if e.get("session_id") != session_id]
    entries.append({
        "session_id": session_id,
        "mode": mode,
        "started_at": datetime.now(timezone.utc).isoformat(),
        **fields,
    })
    _write_active_modes(entries)
    logs.log("active_mode_set", session_id=session_id,
             context={"mode": mode, **fields})


def clear_active_mode(session_id: str) -> bool:
    entries = _read_active_modes_raw()
    new = [e for e in entries if e.get("session_id") != session_id]
    if len(new) == len(entries):
        return False
    _write_active_modes(new)
    logs.log("active_mode_cleared", session_id=session_id)
    return True


def get_active_mode(session_id: str) -> Optional[dict]:
    """Return the (non-stale) active mode entry for `session_id`, or None.

    Entries older than STALE_HOURS are ignored at read time. No cleanup
    job needed — stale entries just accumulate as dead lines in the file
    and the file stays small (one per session, bounded by active users).
    """
    if not session_id:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS)
    for e in reversed(_read_active_modes_raw()):
        if e.get("session_id") != session_id:
            continue
        started_at_str = e.get("started_at", "")
        try:
            started_at = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
        if started_at < cutoff:
            return None  # stale; treat as no active mode
        return e
    return None


def _read_active_modes_raw() -> list[dict]:
    path = _active_modes_path()
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _write_active_modes(entries: list[dict]) -> None:
    path = _active_modes_path()
    body = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)
    if body:
        body += "\n"
    with profile._file_lock(path):
        profile._atomic_write(path, body)


# ---------------------------------------------------------------------------
# Private sessions
# ---------------------------------------------------------------------------


def mark_private(session_id: str) -> None:
    if not session_id:
        return
    current = list_private_session_ids()
    if session_id in current:
        return
    current.add(session_id)
    _write_private_sessions(sorted(current))
    logs.log("session_marked_private", session_id=session_id)


def unmark_private(session_id: str) -> bool:
    current = list_private_session_ids()
    if session_id not in current:
        return False
    current.discard(session_id)
    _write_private_sessions(sorted(current))
    logs.log("session_unmarked_private", session_id=session_id)
    return True


def is_private(session_id: str) -> bool:
    if not session_id:
        return False
    return session_id in list_private_session_ids()


def list_private_session_ids() -> set[str]:
    path = _private_sessions_path()
    if not path.exists():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = entry.get("session_id")
        if sid:
            out.add(sid)
    return out


def _write_private_sessions(session_ids: list[str]) -> None:
    path = _private_sessions_path()
    body = "\n".join(
        json.dumps({"session_id": sid, "ts": datetime.now(timezone.utc).isoformat()},
                    ensure_ascii=False)
        for sid in session_ids
    )
    if body:
        body += "\n"
    with profile._file_lock(path):
        profile._atomic_write(path, body)


# ---------------------------------------------------------------------------
# Pending intents — bridge slash handlers (no session_id) to pre_llm_call
# (has session_id). A handler writes an intent; the next pre_llm_call claims it.
# ---------------------------------------------------------------------------


def push_pending_intent(intent: str, **fields) -> None:
    """Overwrite the pending-intent file with a fresh intent.

    intent: one of "onboarding", "mentoring", "private_on", "private_off".
    fields: any payload (e.g., session_n for onboarding, queue_count for
            mentoring). Stored as-is and passed to the handler that claims.
    """
    path = _pending_intent_path()
    payload = {
        "intent": intent,
        "ts": datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    with profile._file_lock(path):
        profile._atomic_write(path, json.dumps(payload, ensure_ascii=False) + "\n")
    logs.log("pending_intent_pushed", context={"intent": intent, **fields})


def claim_pending_intent(session_id: str,
                          max_age_sec: int = PENDING_INTENT_MAX_AGE_SEC
                          ) -> Optional[dict]:
    """Atomically read, validate, and delete the pending intent.

    Returns the intent dict (with `intent` and any extra fields) if a fresh
    intent exists, otherwise None. After this call, the pending file is gone.
    """
    if not session_id:
        return None
    path = _pending_intent_path()
    if not path.exists():
        return None
    with profile._file_lock(path):
        # Re-check inside the lock to avoid races.
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            return None
        # Validate age.
        ts_str = data.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            path.unlink(missing_ok=True)
            return None
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > max_age_sec:
            path.unlink(missing_ok=True)
            logs.log("pending_intent_expired", context={"age_sec": age, **data})
            return None
        # Claim it — delete the file so no other turn reads it.
        path.unlink(missing_ok=True)
    logs.log("pending_intent_claimed",
             session_id=session_id, context={**data})
    return data

"""The nine tools the LLM can call.

Each tool is a Python function plus a JSON schema describing its
parameters. `register_all(adapter)` wires them all into Hermes via
`adapter.register_tool(...)`.

The tools split into:
  Read:    read_profile, read_playbook, read_queue
  Propose knowledge: propose_addition, flag_contradiction
  Propose action:    propose_action, note_handled
  Apply:   apply_queue_decision, mark_session_complete
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import logs, profile

# Set by `register_all(adapter)` so tools that need adapter access (the
# cron-side ones that send messages or read session history) can reach it
# without threading it through every handler signature.
_adapter: Any = None

# ---------------------------------------------------------------------------
# Valid enum values
# ---------------------------------------------------------------------------

VALID_SOURCE_KINDS = (
    "email", "sms", "chat", "voice_transcript", "meeting_transcript", "document", "other",
)
VALID_URGENCIES = ("low", "medium", "high")
VALID_ACTION_KINDS = (
    "draft_reply", "schedule_event", "create_task", "escalate_to_owner",
    "forward", "record_only", "other",
)
VALID_DECISIONS = ("approve", "edit", "reject")

# ---------------------------------------------------------------------------
# Defensive type coercion
# ---------------------------------------------------------------------------
#
# OpenAI-style tool calls deliver arguments as JSON-decoded values. In
# theory the schema's "type": "integer" / "boolean" / "object" gives the
# LLM the right format; in practice many providers (and some models)
# stringify numerics, JSON-encode nested objects, or pass enum-string
# booleans. The same Hermes dispatcher that bit us with `task_id` will
# happily pass `session_n: "0"` straight through. Coerce defensively at
# the call site so a stringified int doesn't cause a silent ValueError
# that the LLM then narrates over.


def _coerce_int(value: Any, name: str = "value") -> int:
    """Accept int or stringified int. Reject bools (they're int subclass)."""
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer (got bool)")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if s.lstrip("-").isdigit():
            return int(s)
    raise ValueError(f"{name} must be an integer (got {type(value).__name__}: {value!r})")


def _coerce_bool(value: Any, name: str = "value") -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
    raise ValueError(f"{name} must be a boolean (got {type(value).__name__}: {value!r})")


def _coerce_dict(value: Any, name: str = "value") -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        import json
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"{name} must be an object/dict (got string that isn't JSON: {e})"
            ) from None
        if not isinstance(parsed, dict):
            raise ValueError(
                f"{name} must be an object/dict (got JSON {type(parsed).__name__})"
            )
        return parsed
    raise ValueError(f"{name} must be an object/dict (got {type(value).__name__})")


def _coerce_list(value: Any, name: str = "value") -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        # Two shapes: a JSON-encoded list, or a single bare string we
        # wrap as a one-element list (the latter is forgiving — the LLM
        # passing one source instead of a list shouldn't crash the call).
        import json
        s = value.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"{name} must be a list (got string that isn't JSON: {e})"
                ) from None
            if isinstance(parsed, list):
                return parsed
        return [value]
    raise ValueError(f"{name} must be a list (got {type(value).__name__})")


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


def read_profile(section: str) -> str:
    start = time.monotonic()
    try:
        out = profile.read_profile_section(section)
        logs.log("tool_call", tool="read_profile", tool_args={"section": section},
                 ok=True, duration_ms=int((time.monotonic() - start) * 1000))
        return out
    except Exception as e:  # noqa: BLE001
        logs.log_error("tool_call", e, where="read_profile",
                       tool="read_profile", tool_args={"section": section}, ok=False)
        raise


def read_playbook(name: str) -> str:
    start = time.monotonic()
    try:
        out = profile.read_playbook(name)
        logs.log("tool_call", tool="read_playbook", tool_args={"name": name},
                 ok=True, duration_ms=int((time.monotonic() - start) * 1000))
        return out
    except Exception as e:  # noqa: BLE001
        logs.log_error("tool_call", e, where="read_playbook",
                       tool="read_playbook", tool_args={"name": name}, ok=False)
        raise


def read_queue(status: str = "pending", limit: int = 20, queue: str = "review") -> list[dict]:
    start = time.monotonic()
    limit = _coerce_int(limit, "limit")
    try:
        out = profile.read_queue(queue=queue, status=status, limit=limit)
        logs.log("tool_call", tool="read_queue",
                 tool_args={"queue": queue, "status": status, "limit": limit},
                 ok=True, duration_ms=int((time.monotonic() - start) * 1000))
        return out
    except Exception as e:  # noqa: BLE001
        logs.log_error("tool_call", e, where="read_queue",
                       tool="read_queue", ok=False)
        raise


# ---------------------------------------------------------------------------
# Propose tools — knowledge
# ---------------------------------------------------------------------------


def propose_addition(file: str, section: str, content: str, reason: str,
                     source: Optional[str] = None) -> str:
    if file not in profile.PLAYBOOKS:
        raise ValueError(f"unknown file {file!r}; valid: {profile.PLAYBOOKS}")
    if not section or not content:
        raise ValueError("section and content are required")
    # Dedupe: if an identical (file, section, content) is already pending,
    # return the existing id instead of creating a duplicate queue item.
    # Cheap defense against the same conversation getting reflected on twice
    # (e.g., daily cron processes a turn that was also part of the inbound flow).
    for existing in profile.read_queue("review", status="pending", limit=10_000):
        if (existing.get("kind") == "addition"
                and existing.get("file") == file
                and existing.get("section") == section
                and existing.get("content") == content):
            logs.log("propose_addition_deduped",
                     item_id=existing.get("id"),
                     file=file, section=section)
            return existing["id"]
    item = {
        "kind": "addition",
        "file": file,
        "section": section,
        "content": content,
        "reason": reason,
        "source": source or "",
    }
    iid = profile.append_review_item(item)
    logs.log("propose_addition", item_id=iid, file=file, section=section)
    return iid


def flag_contradiction(description: str, sources: list[str]) -> str:
    if not description:
        raise ValueError("description is required")
    sources = _coerce_list(sources, "sources")
    if len(sources) < 2:
        raise ValueError("sources must be a list of two or more file references")
    item = {
        "kind": "contradiction",
        "description": description,
        "sources": sources,
        "content": description,  # so read_queue clients see something descriptive
        "file": None,
        "section": None,
        "reason": "contradiction between captured facts",
    }
    iid = profile.append_review_item(item)
    logs.log("flag_contradiction", item_id=iid, kind="contradiction")
    return iid


# ---------------------------------------------------------------------------
# Propose tools — action
# ---------------------------------------------------------------------------


def propose_action(
    source_kind: str,
    source_id: str,
    source_summary: str,
    first_pass_prediction: str,
    final_recommendation: str,
    reasoning: str,
    urgency: str,
    action_kind: str,
    action_payload: Optional[dict] = None,
    source_channel: Optional[str] = None,
    source_content_excerpt: Optional[str] = None,
    playbooks_consulted: Optional[list[str]] = None,
) -> str:
    if source_kind not in VALID_SOURCE_KINDS:
        raise ValueError(f"bad source_kind {source_kind!r}; valid: {VALID_SOURCE_KINDS}")
    if urgency not in VALID_URGENCIES:
        raise ValueError(f"bad urgency {urgency!r}; valid: {VALID_URGENCIES}")
    if action_kind not in VALID_ACTION_KINDS:
        raise ValueError(f"bad action_kind {action_kind!r}; valid: {VALID_ACTION_KINDS}")
    action_payload = _coerce_dict(action_payload, "action_payload") if action_payload is not None else None
    playbooks_consulted = _coerce_list(playbooks_consulted, "playbooks_consulted") if playbooks_consulted is not None else None

    # De-dupe: if a pending action already exists for the same (source_kind, source_id),
    # update it instead of appending a duplicate.
    existing = profile.find_action_by_source(source_kind, source_id)
    if existing and existing.get("status") == "pending":
        profile.update_queue_item("actions", existing["id"], {
            "first_pass_prediction": first_pass_prediction,
            "final_recommendation": final_recommendation,
            "reasoning": reasoning,
            "urgency": urgency,
            "action_kind": action_kind,
            "action_payload": action_payload or {},
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        logs.log("action_proposed", item_id=existing["id"],
                 action_kind=action_kind, urgency=urgency,
                 duplicate_of=existing["id"])
        return existing["id"]

    item = {
        "source_kind": source_kind,
        "source_id": source_id,
        "source_channel": source_channel or source_kind,
        "source_summary": source_summary,
        "source_content_excerpt": source_content_excerpt or "",
        "first_pass_prediction": first_pass_prediction,
        "final_recommendation": final_recommendation,
        "reasoning": reasoning,
        "playbooks_consulted": playbooks_consulted or [],
        "urgency": urgency,
        "action_kind": action_kind,
        "action_payload": action_payload or {},
        "owner_notified_at": None,
        "owner_notified_via": None,
        "owner_decided_at": None,
        "owner_decision": None,
        "owner_edits": None,
        "last_nudge_at": None,
        "dispatched_at": None,
    }
    iid = profile.append_action_item(item)
    logs.log("action_proposed", item_id=iid, action_kind=action_kind, urgency=urgency)
    return iid


def note_handled(source_kind: str, source_id: str, reason: str) -> bool:
    if source_kind not in VALID_SOURCE_KINDS:
        raise ValueError(f"bad source_kind {source_kind!r}")
    logs.log("inbound_processed", action="noted_no_action",
             source_kind=source_kind, source_id=source_id, reason=reason)
    return True


# ---------------------------------------------------------------------------
# Apply tools
# ---------------------------------------------------------------------------


def apply_queue_decision(item_id: str, decision: str,
                          edited_content: Optional[str] = None) -> bool:
    if decision not in VALID_DECISIONS:
        raise ValueError(f"bad decision {decision!r}; valid: {VALID_DECISIONS}")
    queue = "actions" if item_id.startswith("a_") else "review"
    item = profile.find_queue_item(queue, item_id)
    if not item:
        raise ValueError(f"queue item {item_id!r} not found")
    if decision == "edit" and not edited_content:
        raise ValueError("edit decision requires edited_content")

    kind = item.get("kind") if queue == "review" else "action"

    if queue == "actions":
        # Action items follow the dispatch path. The actual external action is
        # performed by inbound.dispatch_action (called separately so this stays
        # pure I/O).
        if decision == "reject":
            profile.update_queue_item("actions", item_id,
                                       {"status": "rejected",
                                        "owner_decision": "reject",
                                        "owner_decided_at":
                                        datetime.now(timezone.utc).isoformat()})
        else:
            updates: dict[str, Any] = {
                "status": "approved",
                "owner_decision": decision,
                "owner_decided_at": datetime.now(timezone.utc).isoformat(),
            }
            if decision == "edit":
                updates["owner_edits"] = edited_content
            profile.update_queue_item("actions", item_id, updates)
        logs.log("action_decided", item_id=item_id, decision=decision)
        return True

    # Review-queue kinds
    if kind == "addition":
        if decision == "reject":
            profile.update_queue_item("review", item_id, {"status": "rejected"})
        else:
            content = edited_content if decision == "edit" else item["content"]
            profile.insert_into_playbook(item["file"], item["section"], content)
            profile.update_queue_item(
                "review", item_id,
                {"status": "edited" if decision == "edit" else "approved"},
            )
    elif kind == "contradiction":
        if decision == "reject":
            profile.update_queue_item("review", item_id, {"status": "rejected"})
        else:
            # Apply the owner's resolution: add it as a new entry to the first
            # listed source file, under a "Resolutions" section that aggregates
            # owner overrides.
            resolution = edited_content or item.get("description", "")
            sources = item.get("sources") or []
            primary = sources[0].split("#")[0] if sources else None
            if primary and primary.endswith(".md"):
                primary = primary[:-3]
            if primary in profile.PLAYBOOKS:
                profile.insert_into_playbook(
                    primary, f"Owner resolution to {item_id}", resolution
                )
            profile.update_queue_item(
                "review", item_id,
                {"status": "edited" if decision == "edit" else "approved"},
            )
    elif kind == "compression":
        if decision == "reject":
            profile.update_queue_item("review", item_id, {"status": "rejected"})
        else:
            new_content = edited_content if decision == "edit" else item["content"]
            playbook = item["file"]
            profile.archive_playbook_version(playbook)
            profile.write_playbook(playbook, new_content)
            profile.update_queue_item(
                "review", item_id,
                {"status": "edited" if decision == "edit" else "approved"},
            )
    elif kind == "gap":
        # Gaps don't have file content; we just mark the status.
        profile.update_queue_item(
            "review", item_id,
            {"status": {"approve": "approved", "edit": "edited",
                        "reject": "rejected"}[decision]},
        )
    else:
        raise ValueError(f"unknown queue item kind {kind!r}")

    logs.log("queue_decision_applied", item_id=item_id, kind=kind, decision=decision)
    return True


# ---------------------------------------------------------------------------
# Compression — used by the 14 weekly playbook crons + the summary cron
# ---------------------------------------------------------------------------


def propose_compression(file: str, content: str, summary: str,
                         diff: Optional[str] = None) -> str:
    """Queue a compressed rewrite of a playbook for owner review.

    Called by the LLM during a weekly compression cron turn. The owner
    approves/edits/rejects in /mentor.
    """
    if file not in profile.PLAYBOOKS:
        raise ValueError(f"unknown file {file!r}; valid: {profile.PLAYBOOKS}")
    if not content or not summary:
        raise ValueError("content and summary are required")
    item = {
        "kind": "compression",
        "file": file,
        "section": None,
        "content": content,
        "reason": summary,
        "diff": diff or "",
    }
    iid = profile.append_review_item(item)
    logs.log("propose_compression", item_id=iid, file=file)
    return iid


def apply_profile_summary(text: str) -> bool:
    """Write a new profile.yaml.summary.text immediately (no owner review).

    The summary is a derived field — regenerable on the next weekly run.
    Owner review would be friction without value here. Used by the
    Sunday-4:10 summary cron.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("summary text is required")
    profile.update_profile_summary(text)
    logs.log("summary_regenerated")
    return True


# ---------------------------------------------------------------------------
# Cron-side I/O — the daily-cron LLM uses these to walk the inbox + archive
# ---------------------------------------------------------------------------


def list_inbox() -> list[str]:
    """Return the names of files currently in the inbox."""
    inbox = profile.home() / "inbox"
    if not inbox.exists():
        return []
    return sorted(p.name for p in inbox.iterdir() if p.is_file())


def read_inbox_file(name: str, max_chars: int = 60_000) -> str:
    """Return the text content of one inbox file. Caps at max_chars so a
    huge document doesn't blow the LLM's context — the LLM can decide what
    to do with truncation."""
    max_chars = _coerce_int(max_chars, "max_chars")
    if "/" in name or name.startswith(".."):
        raise ValueError("name must be a bare filename, not a path")
    path = profile.home() / "inbox" / name
    if not path.exists():
        raise FileNotFoundError(f"inbox file not found: {name}")
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n[TRUNCATED — file is {len(text)} chars; first {max_chars} shown]"
    return text


def archive_file(name: str, status: str = "processed",
                  error: Optional[str] = None) -> str:
    """Move an inbox file into archive/processed/YYYY-MM-DD/ or
    archive/failed/YYYY-MM-DD/. Idempotent at the filesystem level
    (if the source is already gone, returns the would-be path)."""
    if status not in ("processed", "failed"):
        raise ValueError("status must be 'processed' or 'failed'")
    if "/" in name or name.startswith(".."):
        raise ValueError("name must be a bare filename, not a path")
    src = profile.home() / "inbox" / name
    today = datetime.now(timezone.utc).date().isoformat()
    dest_dir = profile.home() / "archive" / status / today
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    if not src.exists():
        # Already archived. Idempotent.
        return str(dest)
    if dest.exists():
        # Avoid clobber.
        stem, suffix = dest.stem, dest.suffix
        i = 1
        while (dest_dir / f"{stem}.{i}{suffix}").exists():
            i += 1
        dest = dest_dir / f"{stem}.{i}{suffix}"
    import shutil
    shutil.move(str(src), str(dest))
    if error and status == "failed":
        (dest.parent / f"{dest.name}.error.txt").write_text(error, encoding="utf-8")
    logs.log("archive_file", file=name,
             context={"status": status, "dest": str(dest)})
    return str(dest)


# ---------------------------------------------------------------------------
# Conversation history — for the daily reflection cron
# ---------------------------------------------------------------------------


def read_conversations(since_hours: int = 24, limit: int = 50,
                        exclude_private: bool = True) -> list[dict]:
    """Return recent Hermes conversations.

    `exclude_private=True` (default) filters out session IDs the owner
    has marked private via /private.
    """
    since_hours = _coerce_int(since_hours, "since_hours")
    limit = _coerce_int(limit, "limit")
    exclude_private = _coerce_bool(exclude_private, "exclude_private")
    if _adapter is None:
        logs.log("read_conversations_no_adapter", level="WARN")
        return []
    from .session_state import list_private_session_ids
    excluded = list_private_session_ids() if exclude_private else None
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    return _adapter.read_conversations(
        since=since, limit=limit, exclude_session_ids=excluded,
    )


# ---------------------------------------------------------------------------
# Nudge cadence + send_nudge
# ---------------------------------------------------------------------------


# Minimum hours between nudges, by urgency. The first nudge is allowed any
# time after notification (the daily cron runs once a day; first-nudge timing
# is implicitly bounded by the cron schedule).
NUDGE_MIN_INTERVAL_HOURS = {"high": 1, "medium": 4, "low": 12}
NUDGE_MAX = 3


def list_pending_actions_due_for_nudge() -> list[dict]:
    """Return pending action items that are due for a nudge right now.

    Filter rules:
    - status == "pending"
    - owner_notified_at must be set (no nudges for un-notified items)
    - nudge_count < NUDGE_MAX
    - now > last_nudge_at + urgency-specific minimum interval (or never nudged)
    """
    items = profile.read_queue("actions", "pending", limit=10_000)
    now = datetime.now(timezone.utc)
    due: list[dict] = []
    for it in items:
        if not it.get("owner_notified_at"):
            continue
        if it.get("nudge_count", 0) >= NUDGE_MAX:
            continue
        last = it.get("last_nudge_at") or it.get("owner_notified_at")
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        urgency = it.get("urgency", "medium")
        min_hours = NUDGE_MIN_INTERVAL_HOURS.get(urgency,
                                                 NUDGE_MIN_INTERVAL_HOURS["medium"])
        if (now - last_dt).total_seconds() >= min_hours * 3600:
            due.append(it)
    return due


def send_nudge(item_id: str, text: str) -> bool:
    """Send a nudge to the owner about a pending action.

    Enforces the cadence rule — if the item isn't actually due (per
    `list_pending_actions_due_for_nudge`'s criteria), this is a no-op and
    returns False. This is the cadence safeguard against the LLM
    spamming the owner.

    On success: increments nudge_count, sets last_nudge_at. If
    nudge_count hits NUDGE_MAX after the increment, marks the item stale.
    """
    item = profile.find_queue_item("actions", item_id)
    if not item:
        raise ValueError(f"action item {item_id!r} not found")
    if item.get("status") != "pending":
        return False
    # Cadence check — match list_pending_actions_due_for_nudge.
    if item.get("nudge_count", 0) >= NUDGE_MAX:
        return False
    last = item.get("last_nudge_at") or item.get("owner_notified_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            urgency = item.get("urgency", "medium")
            min_hours = NUDGE_MIN_INTERVAL_HOURS.get(urgency,
                                                     NUDGE_MIN_INTERVAL_HOURS["medium"])
            if (datetime.now(timezone.utc) - last_dt).total_seconds() < min_hours * 3600:
                logs.log("send_nudge_too_soon", item_id=item_id,
                         context={"min_hours": min_hours, "urgency": urgency})
                return False
        except (ValueError, AttributeError):
            pass
    # Send via the adapter.
    if _adapter is None:
        logs.log("send_nudge_no_adapter", level="WARN", item_id=item_id)
        return False
    sent_ok = _adapter.send_to_owner(text)
    if not sent_ok:
        # Queue for retry. We still don't bump the counter on send failure
        # — we want the cron to try again later, not skip the nudge.
        _queue_pending_message(text)
        logs.log("send_nudge_queued", item_id=item_id)
        return False
    new_count = item.get("nudge_count", 0) + 1
    updates = {
        "nudge_count": new_count,
        "last_nudge_at": datetime.now(timezone.utc).isoformat(),
    }
    if new_count >= NUDGE_MAX:
        updates["status"] = "stale"
        logs.log("action_stale", item_id=item_id)
    profile.update_queue_item("actions", item_id, updates)
    logs.log("nudge_sent", item_id=item_id, nudge_count=new_count)
    return True


# ---------------------------------------------------------------------------
# Owner messaging — for the LLM to compose proactive notes during a cron turn
# ---------------------------------------------------------------------------


def send_to_owner(text: str, target: Optional[str] = None) -> bool:
    """Push a message to the owner via the configured gateway.

    Falls back to pending_messages.jsonl on failure. The next daily cron
    retries via retry_pending_messages.
    """
    if not text or not text.strip():
        raise ValueError("text is required")
    if _adapter is None:
        _queue_pending_message(text, target)
        return False
    ok = _adapter.send_to_owner(text, target=target)
    if not ok:
        _queue_pending_message(text, target)
    return ok


def retry_pending_messages() -> int:
    """Re-dispatch any messages in pending_messages.jsonl. Returns count sent."""
    if _adapter is None:
        return 0
    path = profile.home() / "pending_messages.jsonl"
    if not path.exists():
        return 0
    import json
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not entries:
        path.unlink(missing_ok=True)
        return 0
    remaining: list[dict] = []
    sent = 0
    for entry in entries:
        ok = _adapter.send_to_owner(entry.get("text", ""),
                                       target=entry.get("target"))
        if ok:
            sent += 1
            logs.log("pending_message_sent",
                     context={"target": entry.get("target")})
        else:
            remaining.append(entry)
    if remaining:
        path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in remaining) + "\n",
                         encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
    return sent


def _queue_pending_message(text: str, target: Optional[str] = None) -> None:
    """Append a failed-to-send message to pending_messages.jsonl."""
    import json
    path = profile.home() / "pending_messages.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "text": text,
        "target": target,
    }
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    path.write_text(existing + json.dumps(entry, ensure_ascii=False) + "\n",
                     encoding="utf-8")


def mark_session_complete(session_n: int, summary: dict) -> bool:
    session_n = _coerce_int(session_n, "session_n")
    summary = _coerce_dict(summary, "summary")
    profile.write_session_summary(session_n, summary)
    logs.log("mark_session_complete", context={"session_n": session_n})
    return True


# ---------------------------------------------------------------------------
# Registration: tool schemas and the register_all entry point
# ---------------------------------------------------------------------------

_SCHEMAS: dict[str, dict] = {
    "read_profile": {
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "enum": list(profile.PROFILE_SECTIONS),
                "description": "Section of profile.yaml to read.",
            }
        },
        "required": ["section"],
    },
    "read_playbook": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "enum": list(profile.PLAYBOOKS),
                "description": "Which playbook to load.",
            }
        },
        "required": ["name"],
    },
    "read_queue": {
        "type": "object",
        "properties": {
            "status": {"type": "string",
                       "enum": ["pending", "approved", "edited", "rejected",
                                "superseded", "stale", "dispatched",
                                "dispatch_failed", "dropped", "all"],
                       "default": "pending"},
            "limit": {"type": "integer", "default": 20},
            "queue": {"type": "string", "enum": ["review", "actions"],
                      "default": "review"},
        },
    },
    "propose_addition": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "enum": list(profile.PLAYBOOKS)},
            "section": {"type": "string"},
            "content": {"type": "string"},
            "reason": {"type": "string"},
            "source": {"type": "string"},
        },
        "required": ["file", "section", "content", "reason"],
    },
    "flag_contradiction": {
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["description", "sources"],
    },
    "propose_action": {
        "type": "object",
        "properties": {
            "source_kind": {"type": "string", "enum": list(VALID_SOURCE_KINDS)},
            "source_id": {"type": "string"},
            "source_summary": {"type": "string"},
            "first_pass_prediction": {"type": "string"},
            "final_recommendation": {"type": "string"},
            "reasoning": {"type": "string"},
            "urgency": {"type": "string", "enum": list(VALID_URGENCIES)},
            "action_kind": {"type": "string", "enum": list(VALID_ACTION_KINDS)},
            "action_payload": {"type": "object"},
            "source_channel": {"type": "string"},
            "source_content_excerpt": {"type": "string"},
            "playbooks_consulted": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["source_kind", "source_id", "source_summary",
                     "first_pass_prediction", "final_recommendation",
                     "reasoning", "urgency", "action_kind"],
    },
    "note_handled": {
        "type": "object",
        "properties": {
            "source_kind": {"type": "string", "enum": list(VALID_SOURCE_KINDS)},
            "source_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["source_kind", "source_id", "reason"],
    },
    "apply_queue_decision": {
        "type": "object",
        "properties": {
            "item_id": {"type": "string"},
            "decision": {"type": "string", "enum": list(VALID_DECISIONS)},
            "edited_content": {"type": "string"},
        },
        "required": ["item_id", "decision"],
    },
    "mark_session_complete": {
        "type": "object",
        "properties": {
            "session_n": {"type": "integer", "minimum": 0, "maximum": 6},
            "summary": {"type": "object"},
        },
        "required": ["session_n", "summary"],
    },
    "propose_compression": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "enum": list(profile.PLAYBOOKS)},
            "content": {"type": "string"},
            "summary": {"type": "string"},
            "diff": {"type": "string"},
        },
        "required": ["file", "content", "summary"],
    },
    "apply_profile_summary": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    "list_inbox": {"type": "object", "properties": {}},
    "read_inbox_file": {
        "type": "object",
        "properties": {"name": {"type": "string"},
                        "max_chars": {"type": "integer", "default": 60000}},
        "required": ["name"],
    },
    "archive_file": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "status": {"type": "string", "enum": ["processed", "failed"]},
            "error": {"type": "string"},
        },
        "required": ["name"],
    },
    "read_conversations": {
        "type": "object",
        "properties": {
            "since_hours": {"type": "integer", "default": 24},
            "limit": {"type": "integer", "default": 50},
            "exclude_private": {"type": "boolean", "default": True},
        },
    },
    "list_pending_actions_due_for_nudge": {
        "type": "object", "properties": {},
    },
    "send_nudge": {
        "type": "object",
        "properties": {
            "item_id": {"type": "string"},
            "text": {"type": "string"},
        },
        "required": ["item_id", "text"],
    },
    "send_to_owner": {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "target": {"type": "string",
                        "description": "platform:channel-id (omit to use the owner's default channel)"},
        },
        "required": ["text"],
    },
    "retry_pending_messages": {"type": "object", "properties": {}},
}

_DESCRIPTIONS = {
    "read_profile": "Read one section of the owner's foundation profile.",
    "read_playbook": "Read one of the fourteen playbooks.",
    "read_queue": "Read pending items from the review queue or the action queue.",
    "propose_addition": "Propose adding a new rule or fact to a playbook. The owner reviews in /mentor.",
    "flag_contradiction": "Flag a contradiction between two captured facts for owner resolution.",
    "propose_action": "After the two-pass thinking on an inbound external message, propose an action for owner approval.",
    "note_handled": "Record that an inbound was considered but no action is needed.",
    "apply_queue_decision": "Apply the owner's approve/edit/reject decision to a queue item.",
    "mark_session_complete": "Finalize one of the seven onboarding sessions.",
    "propose_compression": "Queue a compressed rewrite of a playbook for owner review. Used by the weekly compression crons.",
    "apply_profile_summary": "Write a new profile.yaml.summary.text immediately (no review). Used by the weekly summary cron.",
    "list_inbox": "List file names currently in the inbox.",
    "read_inbox_file": "Read the text content of one inbox file.",
    "archive_file": "Move an inbox file to archive/processed/ or archive/failed/.",
    "read_conversations": "Read recent Hermes conversations (excludes private sessions by default). Used by the daily reflection cron.",
    "list_pending_actions_due_for_nudge": "Return pending action items whose nudge cadence is up.",
    "send_nudge": "Send a nudge message about a pending action. Enforces the urgency-based cadence — no-op if too soon.",
    "send_to_owner": "Push a message to the owner via the configured gateway. Used by the proactive flow.",
    "retry_pending_messages": "Re-dispatch any messages that failed to send earlier.",
}


def register_all(adapter) -> None:  # noqa: ANN001
    """Register every tool with the given Hermes adapter.

    Also stores the adapter at module scope so cron-side tools (which the
    LLM calls during a Hermes cron turn) can reach Hermes APIs that aren't
    available through their args.
    """
    global _adapter
    _adapter = adapter

    funcs = {
        "read_profile": read_profile,
        "read_playbook": read_playbook,
        "read_queue": read_queue,
        "propose_addition": propose_addition,
        "flag_contradiction": flag_contradiction,
        "propose_action": propose_action,
        "note_handled": note_handled,
        "apply_queue_decision": apply_queue_decision,
        "mark_session_complete": mark_session_complete,
        "propose_compression": propose_compression,
        "apply_profile_summary": apply_profile_summary,
        "list_inbox": list_inbox,
        "read_inbox_file": read_inbox_file,
        "archive_file": archive_file,
        "read_conversations": read_conversations,
        "list_pending_actions_due_for_nudge": list_pending_actions_due_for_nudge,
        "send_nudge": send_nudge,
        "send_to_owner": send_to_owner,
        "retry_pending_messages": retry_pending_messages,
    }
    for name, fn in funcs.items():
        adapter.register_tool(
            name=name,
            description=_DESCRIPTIONS[name],
            schema=_SCHEMAS[name],
            handler=_make_handler(fn),
        )


def _make_handler(fn):
    """Wrap a tool function so it accepts a dict of args (Hermes convention).

    Hermes's tools/registry.py:404 calls handlers as `handler(args, **kwargs)`,
    passing context kwargs like `task_id` alongside the args dict. We accept
    and discard those — none of Solomon's tools need them today. Returning
    something other than a string would also be fine (Hermes JSON-encodes
    non-string returns), but most of our tools already return primitives or
    short status messages.
    """
    def handler(args: dict, **_ctx: Any) -> Any:
        return fn(**args)
    handler.__name__ = fn.__name__
    return handler

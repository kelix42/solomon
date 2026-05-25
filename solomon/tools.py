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
from datetime import datetime, timezone
from typing import Any, Optional

from . import logs, profile

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
    if not isinstance(sources, list) or len(sources) < 2:
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


def mark_session_complete(session_n: int, summary: dict) -> bool:
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
}

_DESCRIPTIONS = {
    "read_profile": "Read one section of the owner's foundation profile.",
    "read_playbook": "Read one of the fourteen playbooks (vocabulary, customers, vendors, operations, sales, marketing, finance, people, product, support, legal, technology, strategy, procurement).",
    "read_queue": "Read pending items from the review queue or the action queue.",
    "propose_addition": "Propose adding a new rule or fact to a playbook. The owner will review in /mentor.",
    "flag_contradiction": "Flag a contradiction between two captured facts for owner resolution.",
    "propose_action": "After the two-pass thinking on an inbound external message, propose an action for owner approval.",
    "note_handled": "Record that an inbound was considered but no action is needed (newsletter, OOO reply, etc.).",
    "apply_queue_decision": "Apply the owner's approve/edit/reject decision to a queue item. Used during /mentor.",
    "mark_session_complete": "Finalize one of the seven onboarding sessions and write the summary into profile.yaml.",
}


def register_all(adapter) -> None:  # noqa: ANN001
    """Register every tool with the given Hermes adapter."""
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
    }
    for name, fn in funcs.items():
        adapter.register_tool(
            name=name,
            description=_DESCRIPTIONS[name],
            parameters=_SCHEMAS[name],
            handler=_make_handler(fn),
        )


def _make_handler(fn):
    """Wrap a tool function so it accepts a dict of args (Hermes convention)."""
    def handler(args: dict) -> Any:
        return fn(**args)
    handler.__name__ = fn.__name__
    return handler

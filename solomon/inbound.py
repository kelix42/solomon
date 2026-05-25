"""Proactive inbound flow — post-turn notification + owner-reply handling.

Three responsibilities:

1. dispatch_pending_notifications — called by hooks.post_llm_call after a
   turn finishes. Any pending_actions items written during that turn (i.e.,
   owner_notified_at is None) get a notification pushed to the owner via
   the adapter.

2. parse_owner_decision — called by hooks.pre_llm_call to detect if the
   owner's current message is a reply to a recent notification
   ("approve", "edit: ...", etc.). When matched, the decision is applied
   and any approved action is dispatched.

3. dispatch_action — carries out one approved pending_action by mapping
   its action_kind to a real-world side effect (send a reply, mark a task,
   etc.). Updates the item's status.

The nudge loop lives in tools.py now — the daily cron's LLM calls
list_pending_actions_due_for_nudge() + send_nudge() itself.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from . import logs, profile, tools


# ---------------------------------------------------------------------------
# Notification format and dispatch
# ---------------------------------------------------------------------------


def _format_notification(item: dict) -> str:
    """Render a pending_action item as the message Solomon sends to the owner."""
    urgency = item.get("urgency", "medium").upper()
    src = item.get("source_kind", "?")
    summary = item.get("source_summary", "?")
    rec = item.get("final_recommendation", "?")
    reason = item.get("reasoning", "?")
    return (
        f"Solomon — pending action ({urgency} urgency)\n"
        f"Source: {src}\n"
        f"About: {summary}\n\n"
        f"I'd: {rec}\n"
        f"Why: {reason}\n\n"
        f"Reply: approve / reject / edit: <your version>"
    )


def dispatch_pending_notifications() -> int:
    """For every pending_actions item with owner_notified_at=None, send a
    notification to the owner's home channel and update the item.

    Returns the number of notifications sent (or queued for retry).

    Uses tools._adapter (the module-level adapter set by tools.register_all).
    """
    if tools._adapter is None:
        logs.log("dispatch_no_adapter", level="WARN")
        return 0
    items = profile.read_queue("actions", status="pending", limit=200)
    sent = 0
    for item in items:
        if item.get("owner_notified_at"):
            continue
        text = _format_notification(item)
        ok = False
        try:
            ok = tools._adapter.send_to_owner(text)
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where="inbound.dispatch_pending_notifications")
        if not ok:
            # Queue for retry via tools._queue_pending_message — same path
            # send_to_owner and send_nudge use.
            tools._queue_pending_message(text)
        now = datetime.now(timezone.utc).isoformat()
        profile.update_queue_item("actions", item["id"], {
            "owner_notified_at": now,
            "owner_notified_via": "owner_home_channel" if ok else "queued",
        })
        logs.log("action_notified", item_id=item["id"], ok=ok)
        sent += 1
    return sent


# ---------------------------------------------------------------------------
# Owner-reply parsing
# ---------------------------------------------------------------------------


# Reply shapes we recognize:
#   "approve" / "approve a_..."
#   "reject"  / "reject a_..."
#   "edit: <text>" / "edit a_...: <text>"
_DECISION_RE = re.compile(
    r"^\s*(approve|reject|edit)(?:\s+(a_\S+))?\s*(?::\s*(.+))?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def parse_owner_decision(text: str) -> Optional[tuple[str, str, Optional[str]]]:
    """Return (item_id, decision, edited_content) or None.

    If the owner's reply doesn't reference an id explicitly, the decision
    attaches to the most-recently-notified pending action.
    """
    if not isinstance(text, str):
        return None
    m = _DECISION_RE.match(text.strip())
    if not m:
        return None
    decision = m.group(1).lower()
    item_id = m.group(2)
    edited = m.group(3)
    if not item_id:
        items = [i for i in profile.read_queue("actions", "pending", limit=100)
                 if i.get("owner_notified_at")]
        if not items:
            return None
        item_id = items[-1]["id"]
    return item_id, decision, edited


def apply_owner_decision(item_id: str, decision: str,
                          edited_content: Optional[str] = None) -> bool:
    """Apply the decision via tools.apply_queue_decision and, if approved,
    dispatch the action."""
    tools.apply_queue_decision(item_id, decision, edited_content)
    if decision in ("approve", "edit"):
        item = profile.find_queue_item("actions", item_id)
        if item:
            dispatch_action(item)
    return True


# ---------------------------------------------------------------------------
# Action dispatching
# ---------------------------------------------------------------------------


def dispatch_action(item: dict) -> bool:
    """Carry out one approved pending_action.

    Returns True if the action succeeded (or is a no-op record). Updates
    the item's status (dispatched | dispatch_failed). Errors are logged
    but never bubble up.
    """
    if item.get("status") not in ("approved",):
        logs.log("action_dispatch_skipped", item_id=item.get("id"),
                 context={"status": item.get("status")})
        return False

    action_kind = item.get("action_kind", "")
    payload = item.get("action_payload") or {}
    if item.get("owner_decision") == "edit" and item.get("owner_edits"):
        # Owner-edited content overrides the LLM's draft. For draft_reply
        # this typically replaces payload["body"]; for other kinds we stuff
        # it into a generic "text" slot.
        payload = dict(payload)
        if "body" in payload:
            payload["body"] = item["owner_edits"]
        else:
            payload["text"] = item["owner_edits"]

    try:
        ok = _carry_out(action_kind, payload, item)
    except Exception as e:  # noqa: BLE001
        logs.log_error("action_dispatch_failed", e,
                        where="inbound.dispatch_action",
                        item_id=item["id"], action_kind=action_kind)
        profile.update_queue_item("actions", item["id"],
                                    {"status": "dispatch_failed"})
        return False

    if ok:
        profile.update_queue_item("actions", item["id"], {
            "status": "dispatched",
            "dispatched_at": datetime.now(timezone.utc).isoformat(),
        })
        logs.log("action_dispatched", item_id=item["id"], action_kind=action_kind)
    else:
        profile.update_queue_item("actions", item["id"],
                                    {"status": "dispatch_failed"})
        logs.log("action_dispatch_failed", item_id=item["id"],
                 action_kind=action_kind)
    return ok


def _carry_out(action_kind: str, payload: dict, item: dict) -> bool:
    """Translate action_kind into the appropriate side effect.

    Most kinds delegate to tools.send_to_owner (which wraps Hermes's
    send_message_tool — capable of targeting any configured channel,
    not just the owner). For action_kinds we haven't wired up yet
    (schedule_event, create_task), we mark dispatched without a real
    external call and surface a note to the owner.
    """
    if action_kind in ("record_only", "escalate_to_owner"):
        # Nothing external to do. The original notification already
        # surfaced the situation to the owner.
        return True
    if action_kind == "draft_reply":
        target = payload.get("target") or _resolve_reply_target(item)
        body = payload.get("body") or payload.get("text") or ""
        subject = payload.get("subject", "")
        full_text = f"Subject: {subject}\n\n{body}" if subject else body
        return tools.send_to_owner(full_text, target=target)
    if action_kind == "forward":
        target = payload.get("to") or payload.get("target")
        note = payload.get("note") or ""
        original = item.get("source_content_excerpt", "")
        full_text = f"{note}\n\n--- Forwarded ---\n{original}".strip()
        return tools.send_to_owner(full_text, target=target)
    if action_kind in ("schedule_event", "create_task"):
        # Not yet wired to real calendar / task tools. Mark dispatched
        # but tell the owner the gap.
        logs.log("action_handler_pending_integration",
                 level="WARN", action_kind=action_kind,
                 item_id=item.get("id"))
        return True
    # Unknown kind.
    logs.log("action_handler_unknown", level="WARN",
             action_kind=action_kind, item_id=item.get("id"))
    return False


def _resolve_reply_target(item: dict) -> Optional[str]:
    """Best-effort: derive a reply target from the source.

    For email-shaped inbounds: 'email:<source_id_or_from>'. For chat
    inbounds: the platform + chat id from source_channel/source_id.

    Returns None if we can't infer — the caller will then send to the
    owner's home channel as a fallback, which surfaces the action for
    manual handling rather than dropping it.
    """
    source_channel = item.get("source_channel") or ""
    source_id = item.get("source_id") or ""
    if not source_channel or not source_id:
        return None
    return f"{source_channel}:{source_id}"

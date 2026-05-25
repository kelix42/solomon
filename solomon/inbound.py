"""Proactive inbound flow.

Five responsibilities, all small:

  1. dispatch_pending_notifications: after a turn writes new items to
     pending_actions.jsonl, send the owner a one-shot notification on
     their preferred channel. Called from hooks.post_llm_call.

  2. parse_owner_decision: when the owner's reply is one of "approve",
     "reject", "edit: ...", match it to the most recently-notified
     pending action and apply via tools.apply_queue_decision. Called by
     hooks.pre_llm_call before injecting the default system prompt.

  3. dispatch_action: carry out an approved pending action. Used by the
     daily cron when the owner has approved an item that wasn't sent yet
     (rare race condition) and by parse_owner_decision after approval.

  4. compose_nudge: produce a one-sentence nudge for an item the owner
     has been ignoring. Used by daily.py's nudge step.

  5. retry_pending_messages: dispatch any queued messages that failed to
     send last time. Used by daily.py.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from . import logs, profile, tools

PENDING_MSGS_FILE = "pending_messages.jsonl"


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


def _preferred_channel() -> Optional[str]:
    """From profile.yaml.meta.preferred_channel. Empty string → None."""
    path = profile.home() / "profile.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    ch = (data.get("meta") or {}).get("preferred_channel") or ""
    return ch.strip() or None


def _queue_pending_message(text: str, channel: Optional[str]) -> None:
    path = profile.home() / PENDING_MSGS_FILE
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "text": text,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    path.write_text(existing + json.dumps(entry, ensure_ascii=False) + "\n",
                     encoding="utf-8")


def dispatch_pending_notifications(*, session=None, adapter=None) -> int:
    """For every pending_actions item with owner_notified_at=None, send
    a notification on the preferred channel and update the item.

    Returns the number of notifications sent (or queued for retry).
    """
    if adapter is None:
        # Try to find the adapter on the session — Hermes may stash it there.
        adapter = getattr(session, "adapter", None)
    items = profile.read_queue("actions", status="pending", limit=200)
    sent = 0
    channel = _preferred_channel()
    for item in items:
        if item.get("owner_notified_at"):
            continue
        text = _format_notification(item)
        sent_ok = False
        if adapter is not None:
            try:
                sent_ok = adapter.send_to_owner(text, channel=channel)
            except Exception as e:  # noqa: BLE001
                logs.log_error("error", e, where="inbound.dispatch_pending_notifications")
                sent_ok = False
        if not sent_ok:
            _queue_pending_message(text, channel)
        now = datetime.now(timezone.utc).isoformat()
        profile.update_queue_item("actions", item["id"], {
            "owner_notified_at": now,
            "owner_notified_via": channel or "queued",
        })
        logs.log("action_notified",
                 item_id=item["id"],
                 channel=channel or "queued",
                 ok=sent_ok)
        sent += 1
    return sent


# ---------------------------------------------------------------------------
# Owner decision parsing
# ---------------------------------------------------------------------------


# Lightweight matchers. The owner's reply formats we recognize:
#   "approve" / "approve a_..."
#   "reject"  / "reject a_..."
#   "edit: <text>" / "edit a_...: <text>"
_DECISION_RE = re.compile(
    r"^\s*(approve|reject|edit)(?:\s+(a_\S+))?\s*(?::\s*(.+))?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def parse_owner_decision(text: str) -> Optional[tuple[str, str, Optional[str]]]:
    """Return (item_id, decision, edited_content) or None.

    If the owner's reply doesn't reference an id explicitly, we attach it
    to the most-recent pending notified action.
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
        # Find the latest pending action that was notified.
        items = profile.read_queue("actions", status="pending", limit=100)
        items = [i for i in items if i.get("owner_notified_at")]
        if not items:
            return None
        item_id = items[-1]["id"]
    return item_id, decision, edited


def apply_owner_decision(item_id: str, decision: str,
                          edited_content: Optional[str], *, adapter=None) -> bool:
    """Apply the decision and (if approved) dispatch the action."""
    tools.apply_queue_decision(item_id, decision, edited_content)
    if decision in ("approve", "edit"):
        item = profile.find_queue_item("actions", item_id)
        if item:
            dispatch_action(item, adapter=adapter)
    return True


# ---------------------------------------------------------------------------
# Action dispatching
# ---------------------------------------------------------------------------


def dispatch_action(item: dict, *, adapter=None) -> bool:
    """Carry out an approved pending action.

    Returns True on success, False on failure. Updates the item's status.
    """
    if item.get("status") not in ("approved",):
        logs.log("action_dispatch_skipped",
                 item_id=item["id"],
                 reason=f"status={item.get('status')}")
        return False

    action_kind = item.get("action_kind", "")
    payload = item.get("action_payload") or {}
    # If the owner edited the recommendation, prefer the edited content.
    if item.get("owner_decision") == "edit" and item.get("owner_edits"):
        # owner_edits is typically a string that replaces action_payload.body
        # for draft_reply, or the whole task title for create_task, etc.
        # We let the LLM/owner decide via free text and store it as-is.
        if "body" in payload:
            payload = dict(payload)
            payload["body"] = item["owner_edits"]
        else:
            payload = {"text": item["owner_edits"], **payload}

    try:
        ok = _carry_out(action_kind, payload, adapter=adapter)
    except Exception as e:  # noqa: BLE001
        logs.log_error("action_dispatch_failed", e, where="inbound.dispatch_action",
                        item_id=item["id"], action_kind=action_kind)
        profile.update_queue_item("actions", item["id"], {"status": "dispatch_failed"})
        return False

    if ok:
        profile.update_queue_item("actions", item["id"], {
            "status": "dispatched",
            "dispatched_at": datetime.now(timezone.utc).isoformat(),
        })
        logs.log("action_dispatched", item_id=item["id"], action_kind=action_kind)
    else:
        profile.update_queue_item("actions", item["id"], {"status": "dispatch_failed"})
        logs.log("action_dispatch_failed", item_id=item["id"], action_kind=action_kind)
    return ok


def _carry_out(action_kind: str, payload: dict, *, adapter=None) -> bool:
    """Translate action_kind into the appropriate Hermes call.

    Most kinds need a Hermes tool (email send, calendar create, etc.). If
    the adapter doesn't expose one for this kind, we record_only and
    surface the action to the owner for manual follow-up.
    """
    if action_kind == "record_only":
        return True
    if action_kind == "escalate_to_owner":
        # The owner already sees this via the original notification; the
        # "dispatch" is just acknowledging we logged it.
        return True
    if adapter is None:
        # In tests or when running without Hermes, mark as dispatched without
        # an external call. The action_payload is preserved in the log.
        logs.log("action_dispatch_noop", action_kind=action_kind,
                 context={"payload_keys": list(payload.keys())})
        return True
    # For real channels, look for a method on the adapter named after the
    # action_kind. The adapter is intentionally permissive.
    candidates = {
        "draft_reply": ("send_reply", "send_email", "send_message"),
        "schedule_event": ("create_event", "schedule_event"),
        "create_task": ("create_task",),
        "forward": ("forward_message",),
    }
    for fn_name in candidates.get(action_kind, ()):
        fn = getattr(adapter.ctx, fn_name, None)
        if fn is None:
            continue
        try:
            fn(**payload)
            return True
        except TypeError:
            try:
                fn(payload)
                return True
            except Exception:  # noqa: BLE001
                continue
    # No handler available — log and surface.
    logs.log("action_handler_missing", action_kind=action_kind, level="WARN")
    return False


# ---------------------------------------------------------------------------
# Nudges and message retry
# ---------------------------------------------------------------------------


_CADENCE_HOURS = {
    "high": [1, 3, 5],      # nudge 1h, 3h, 5h after notification
    "medium": [4, 10, 16],
    "low": [12, 24, 48],
}


def _next_nudge_due(item: dict) -> Optional[datetime]:
    """When the next nudge for this item is due, given its urgency + history."""
    urgency = item.get("urgency", "medium")
    schedule = _CADENCE_HOURS.get(urgency, _CADENCE_HOURS["medium"])
    notified_at = item.get("owner_notified_at")
    if not notified_at:
        return None
    try:
        base = datetime.fromisoformat(notified_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    nudge_count = item.get("nudge_count", 0)
    if nudge_count >= len(schedule):
        return None  # exhausted
    from datetime import timedelta
    return base + timedelta(hours=schedule[nudge_count])


def _max_nudges() -> int:
    data = yaml.safe_load((profile.home() / "profile.yaml").read_text())
    return ((data or {}).get("meta") or {}).get("nudge_cadence", {}).get("max_nudges", 3)


def compose_nudge(item: dict, *, adapter=None) -> str:
    """Return the one-sentence nudge to send the owner.

    If an adapter+LLM is available, ask the LLM. Otherwise return a default
    deterministic nudge.
    """
    base = (
        f"Hey — still waiting on the {item.get('action_kind', '')} for "
        f"{item.get('source_summary', '')}. Want me to go ahead with the proposal?"
    )
    if adapter is None:
        return base
    system = (
        "You are Solomon nudging the owner about a pending action. Write ONE "
        "short, warm sentence inviting them to decide. No filler. No preamble. "
        "Just the sentence itself, suitable to send through their chat."
    )
    user_msg = (
        f"Pending action: {item.get('source_summary', '')}\n"
        f"My recommendation: {item.get('final_recommendation', '')}\n"
        f"Notified at: {item.get('owner_notified_at', '')}\n"
        f"Nudges sent so far: {item.get('nudge_count', 0)}\n"
        f"Urgency: {item.get('urgency', 'medium')}"
    )
    try:
        text = adapter.llm_call(system=system,
                                  messages=[{"role": "user", "content": user_msg}],
                                  max_tokens=80)
        return text.strip() or base
    except Exception as e:  # noqa: BLE001
        logs.log_error("error", e, where="inbound.compose_nudge")
        return base


def nudge_step(*, adapter=None) -> dict:
    """Process pending actions: send due nudges, mark stale, return counts."""
    items = profile.read_queue("actions", status="pending", limit=200)
    sent = 0
    stale = 0
    now = datetime.now(timezone.utc)
    max_n = _max_nudges()
    channel = _preferred_channel()
    for item in items:
        due = _next_nudge_due(item)
        if due is None:
            # Either not notified yet, or schedule exhausted.
            if item.get("nudge_count", 0) >= max_n:
                profile.update_queue_item("actions", item["id"], {"status": "stale"})
                logs.log("action_stale", item_id=item["id"])
                stale += 1
            continue
        if now < due:
            continue
        text = compose_nudge(item, adapter=adapter)
        sent_ok = False
        if adapter is not None:
            try:
                sent_ok = adapter.send_to_owner(text, channel=channel)
            except Exception as e:  # noqa: BLE001
                logs.log_error("error", e, where="inbound.nudge_step")
                sent_ok = False
        if not sent_ok:
            _queue_pending_message(text, channel)
        profile.update_queue_item("actions", item["id"], {
            "nudge_count": item.get("nudge_count", 0) + 1,
            "last_nudge_at": now.isoformat(),
        })
        logs.log("nudge_sent", item_id=item["id"],
                  nudge_count=item.get("nudge_count", 0) + 1,
                  channel=channel or "queued")
        sent += 1
        # After incrementing, if we just hit max, mark stale on next pass.
        if item.get("nudge_count", 0) + 1 >= max_n:
            profile.update_queue_item("actions", item["id"], {"status": "stale"})
            logs.log("action_stale", item_id=item["id"])
            stale += 1
    return {"sent": sent, "stale": stale}


def retry_pending_messages(*, adapter=None) -> int:
    """Re-dispatch any messages in pending_messages.jsonl. Returns count sent."""
    path = profile.home() / PENDING_MSGS_FILE
    if not path.exists() or adapter is None:
        return 0
    entries = []
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
        ok = False
        try:
            ok = adapter.send_to_owner(entry["text"], channel=entry.get("channel"))
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where="inbound.retry_pending_messages")
            ok = False
        if ok:
            sent += 1
            logs.log("pending_message_sent", channel=entry.get("channel"))
        else:
            remaining.append(entry)
    if remaining:
        path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in remaining) + "\n",
                         encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
    return sent

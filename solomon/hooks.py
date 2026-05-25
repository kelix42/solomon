"""Hermes lifecycle hooks.

Real Hermes hook contract (verified against hermes_cli/plugins.py:1495-1529):

  pre_llm_call(*, session_id, user_message, conversation_history,
               is_first_turn, model, platform, **_)
      -> returns one of:
           None            — no context injection (skip)
           str             — context string injected into user message
           {"context": ..} — same, dict form

      Context is ALWAYS injected into the user message, never the system
      prompt. This preserves Hermes's prompt-cache prefix.

  post_llm_call(*, session_id, model, platform, **_)
      -> return value ignored

  on_session_start(*, session_id, **_)
      -> return value ignored

This module is the only place outside adapter.py that touches Hermes hook
names — it uses the constants from adapter.HOOK_*.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml

from . import adapter, logs, profile, session_state

# Skill files are part of the solomon package; we read them at hook time.
SKILL_DIR = Path(__file__).parent / "skills"


# In-memory caches — reload if mtime changes.
_default_skill_cache: dict[str, Any] = {"mtime": 0.0, "body": ""}
_interview_skill_cache: dict[str, Any] = {"mtime": 0.0, "body": ""}
_vocab_cache: dict[str, Any] = {"mtime": 0.0, "body": ""}


def _load_skill_body(name: str, cache: dict) -> str:
    path = SKILL_DIR / f"{name}.md"
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return ""
    if cache["mtime"] == mtime and cache["body"]:
        return cache["body"]
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---\n", 3)
        if end != -1:
            text = text[end + 5:]
    cache["mtime"] = mtime
    cache["body"] = text.strip()
    return cache["body"]


def _load_default_skill() -> str:
    return _load_skill_body("solomon-default", _default_skill_cache)


def _load_interview_skill() -> str:
    return _load_skill_body("solomon-interview", _interview_skill_cache)


def _load_vocabulary() -> str:
    path = profile.home() / "vocabulary.md"
    if not path.exists():
        return ""
    mtime = path.stat().st_mtime
    if _vocab_cache["mtime"] == mtime and _vocab_cache["body"]:
        return _vocab_cache["body"]
    text = path.read_text(encoding="utf-8")
    _vocab_cache["mtime"] = mtime
    _vocab_cache["body"] = text
    return text


def _load_profile_summary() -> tuple[str, bool]:
    """Return (summary_text, is_empty_profile)."""
    path = profile.home() / "profile.yaml"
    if not path.exists():
        return "", True
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return "(profile.yaml unreadable)", True
    summary = (data.get("summary") or {}).get("text", "")
    meta_last = (data.get("meta") or {}).get("last_updated")
    industry_filled = (data.get("industry") or {}).get("filled", False)
    is_empty = not meta_last and not industry_filled
    return summary, is_empty


def _tool_menu() -> str:
    return (
        "Available tools (call them like JSON-schema args):\n"
        "- read_playbook(name) — load one playbook. Names: customers, vendors,\n"
        "  operations, sales, marketing, finance, people, product, support,\n"
        "  legal, technology, strategy, procurement.\n"
        "- read_profile(section) — load one foundation section.\n"
        "- read_queue(status, limit) — read items from the review queue.\n"
        "- propose_addition(file, section, content, reason) — propose a new\n"
        "  capture for owner review.\n"
        "- flag_contradiction(description, sources) — flag a contradiction.\n"
        "- propose_action(...) — propose an action on an inbound external\n"
        "  message after the two-pass thinking.\n"
        "- note_handled(source_kind, source_id, reason) — record that an\n"
        "  inbound was considered and no action was needed.\n"
        "- apply_queue_decision(item_id, decision, edited_content) — used\n"
        "  during /mentor to apply owner decisions.\n"
        "- mark_session_complete(session_n, summary) — finalize an\n"
        "  onboarding session."
    )


# ---------------------------------------------------------------------------
# Inbound detection
# ---------------------------------------------------------------------------


def _detect_inbound(user_message: Any, platform: str) -> tuple[bool, str, str]:
    """Decide whether the current user_message came from an external
    gateway (vs the owner typing directly).

    Returns (is_inbound, kind, source_id).

    user_message can be a string or a dict carrying gateway metadata; we
    look at both. The platform kwarg is what Hermes attaches to every
    pre_llm_call call.
    """
    # If platform tells us this is a gateway message, that's the strongest
    # signal. CLI / TUI is "owner direct."
    p = (platform or "").lower()
    if p in ("", "cli", "tui", "direct"):
        return False, "", ""

    # Try to pull a stable identifier out of the user_message metadata.
    source_id = ""
    if isinstance(user_message, dict):
        source_id = (user_message.get("message_id") or user_message.get("id")
                      or user_message.get("Message-ID") or "")

    # Map Hermes platform name to our source_kind taxonomy.
    kind_map = {
        "telegram": "chat",
        "slack": "chat",
        "discord": "chat",
        "sms": "sms",
        "signal": "sms",
        "whatsapp": "sms",
        "email": "email",
    }
    kind = kind_map.get(p, "other")
    return True, kind, str(source_id) if source_id else ""


# ---------------------------------------------------------------------------
# Hook callbacks — real Hermes signatures (kwargs-only)
# ---------------------------------------------------------------------------


def _solomon_off() -> bool:
    return (profile.home() / ".solomon_off").exists()


def pre_llm_call(*, session_id: str = "", user_message: Any = None,
                  conversation_history: Any = None,
                  is_first_turn: bool = False, model: str = "",
                  platform: str = "", **_: Any) -> Optional[dict]:
    """Return a context dict for Hermes to inject into the user message.

    Bypasses (return None):
    - .solomon_off sentinel present.
    - session_id is in private_sessions.jsonl.
    """
    if _solomon_off():
        return None

    # Claim any pending intent left by a slash handler in this conversation.
    # Slash handlers don't get a session_id; this is how the intent reaches
    # the correct session.
    pending = session_state.claim_pending_intent(session_id)
    if pending:
        intent = pending.get("intent")
        if intent == "onboarding":
            session_state.set_active_mode(
                session_id, "onboarding",
                session_n=pending.get("session_n"),
            )
        elif intent == "mentoring":
            session_state.set_active_mode(
                session_id, "mentoring",
                queue_count=pending.get("queue_count"),
                action_count=pending.get("action_count"),
            )
        elif intent == "private_on":
            session_state.mark_private(session_id)
        elif intent == "private_off":
            session_state.unmark_private(session_id)

    if session_state.is_private(session_id):
        # The conversation continues; Solomon just doesn't inject anything.
        # Daily reflection will skip this session entirely.
        return None

    active = session_state.get_active_mode(session_id)
    summary, is_empty = _load_profile_summary()
    vocab = _load_vocabulary().strip() or "(vocabulary file is empty — capture phrases as you hear them)"
    is_inbound, kind, source_id = _detect_inbound(user_message, platform)

    if active is not None:
        # Onboarding / mentoring / checkin — load the interview skill.
        skill = _load_interview_skill()
        mode = active.get("mode", "")
        meta_lines = [f"MODE: {mode}"]
        if mode == "onboarding":
            n = active.get("session_n")
            if n is not None:
                meta_lines.append(f"SESSION: {n} ({profile.SESSION_NAMES.get(n, '?')})")
                fields = profile.SESSION_REQUIRED_FIELDS.get(n, ())
                meta_lines.append(f"REQUIRED FIELDS: {', '.join(fields)}")
        elif mode == "mentoring":
            qc = active.get("queue_count")
            ac = active.get("action_count")
            if qc is not None:
                meta_lines.append(f"REVIEW QUEUE PENDING: {qc}")
            if ac is not None:
                meta_lines.append(f"ACTIONS NEEDING ATTENTION: {ac}")
        block = (
            "# Solomon interview role (active)\n\n" + skill +
            "\n\n# Active mode\n\n" + "\n".join(meta_lines) +
            "\n\n# Owner vocabulary\n\n" + vocab +
            "\n\n# Profile summary\n\n" + (summary or "(profile is empty)") +
            "\n\n" + _tool_menu()
        )
        logs.log("skill_loaded", skill="solomon-interview",
                 session_id=session_id, context={"mode": mode})
        return {"context": block}

    # Default role on every other turn.
    skill = _load_default_skill()
    block_parts = [
        "# Solomon role (loaded on every turn)\n",
        skill,
        "\n# Owner vocabulary\n",
        vocab,
        "\n# Profile summary\n",
        summary.strip() if summary and summary.strip() else "(profile is empty — first turn invite the owner to /onboard)",
        "\n",
        _tool_menu(),
    ]
    if is_empty:
        block_parts.append("\nNOTE: profile.yaml is empty. Invite the owner to run /onboard.")
    if is_inbound:
        block_parts.append(
            f"\nINBOUND CONTEXT: This message is from an external source via {platform} "
            f"(source_kind={kind!r}, source_id={source_id!r}). Apply the two-pass inbound "
            "flow per your skill instructions: gut-check first, then load relevant playbooks "
            "and refine, then call propose_action or note_handled."
        )
    logs.log("skill_loaded", skill="solomon-default", session_id=session_id,
             context={"inbound": is_inbound, "kind": kind})
    return {"context": "\n".join(block_parts)}


def post_llm_call(*, session_id: str = "", model: str = "",
                   platform: str = "", **_: Any) -> None:
    """After every Hermes turn. Log and trigger inbound-notification dispatch."""
    if session_state.is_private(session_id):
        logs.log("private_turn", session_id=session_id)
        return
    logs.log("turn_end", session_id=session_id, model=model)
    # Dispatch any pending notifications written this turn. The dispatcher
    # is in solomon.inbound; lazy-imported to avoid circulars at plugin load.
    try:
        from . import inbound
        inbound.dispatch_pending_notifications()
    except Exception as e:  # noqa: BLE001
        logs.log_error("error", e, where="hooks.post_llm_call.dispatch")


def on_session_start(*, session_id: str = "", **_: Any) -> None:
    logs.log("session_start", session_id=session_id)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_all(adapter_obj) -> None:  # noqa: ANN001
    adapter_obj.register_hook(adapter.HOOK_PRE_LLM_CALL, pre_llm_call)
    adapter_obj.register_hook(adapter.HOOK_POST_LLM_CALL, post_llm_call)
    adapter_obj.register_hook(adapter.HOOK_ON_SESSION_START, on_session_start)

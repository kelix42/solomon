"""Hermes lifecycle hooks.

pre_llm_call:  injects the Solomon role on every turn (unless /private or
               /solomon-off is in effect), and flags inbound external
               messages so the LLM applies the two-pass flow.

post_llm_call: dispatches any pending-action notifications the LLM just
               wrote during the turn.

on_session_start: initializes per-session state (private flag, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import logs, profile

SKILL_PATH = Path(__file__).parent / "skills" / "solomon-default.md"


# In-memory caches; reloaded if mtime changes.
_skill_cache: dict[str, Any] = {"mtime": 0.0, "body": ""}
_vocab_cache: dict[str, Any] = {"mtime": 0.0, "body": ""}


def _load_default_skill() -> str:
    """Return just the markdown body (front matter stripped)."""
    mtime = SKILL_PATH.stat().st_mtime
    if _skill_cache["mtime"] == mtime and _skill_cache["body"]:
        return _skill_cache["body"]
    text = SKILL_PATH.read_text(encoding="utf-8")
    # Strip the YAML front matter.
    if text.startswith("---"):
        end = text.find("\n---\n", 3)
        if end != -1:
            text = text[end + 5 :]
    _skill_cache["mtime"] = mtime
    _skill_cache["body"] = text.strip()
    return _skill_cache["body"]


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
        "Available tools:\n"
        "- read_playbook(name) — load one playbook on demand. Names: "
        "customers, vendors, operations, sales, marketing, finance, people, "
        "product, support, legal, technology, strategy, procurement.\n"
        "- read_profile(section) — load one foundation section on demand. "
        "Sections: industry, belief_system, why, principles, ideal_outcomes, "
        "non_negotiables, scopes, meta.\n"
        "- read_queue(status, limit) — read items from the review queue.\n"
        "- propose_addition(file, section, content, reason) — propose a new "
        "capture for owner review.\n"
        "- flag_contradiction(description, sources) — flag a contradiction.\n"
        "- propose_action(...) — propose an action on an inbound external "
        "message after the two-pass thinking.\n"
        "- note_handled(source_kind, source_id, reason) — record that an "
        "inbound was considered and no action was needed.\n"
        "- apply_queue_decision(item_id, decision, edited_content) — used "
        "during /mentor to apply owner decisions.\n"
        "- mark_session_complete(session_n, summary) — finalize an "
        "onboarding session."
    )


def _solomon_off() -> bool:
    return (profile.home() / ".solomon_off").exists()


def is_session_private(session: Any) -> bool:
    return bool(getattr(session, "private", False))


def is_external_inbound(messages: list[dict], session: Any) -> tuple[bool, str, str, str]:
    """Detect whether the latest user message came from an external gateway.

    Returns (is_inbound, source_kind, source_id, source_channel).
    is_inbound=False means the owner typed it directly.
    """
    if not messages:
        return False, "", "", ""
    last = messages[-1]
    # Hermes may put gateway info on the message itself or on the session.
    source = (last.get("source") or last.get("sender") or {}) if isinstance(last, dict) else {}
    session_source = getattr(session, "source", None)
    if isinstance(source, str):
        source = {"kind": source}
    if not source and session_source:
        source = session_source if isinstance(session_source, dict) else {"kind": str(session_source)}
    kind = (source.get("kind") or source.get("channel") or "").lower()
    # If the message has an "is_from_owner" hint, respect it.
    if last.get("is_from_owner") is True:
        return False, "", "", ""
    # CLI / direct chat is not external.
    if kind in ("", "cli", "owner", "user", "direct"):
        return False, "", "", ""
    sid = (source.get("id") or last.get("id") or last.get("message_id")
           or last.get("Message-ID") or "")
    channel = source.get("channel") or kind
    return True, kind, str(sid), channel


def pre_llm_call(messages: list[dict], session: Any) -> None:
    """Inject Solomon's default context into every Hermes turn.

    Bypasses:
    - .solomon_off sentinel: skip everything.
    - session.private: skip everything for this conversation.
    - session.solomon_skill_overridden: a slash command is providing its own
      complete system prompt; let it through untouched.
    """
    if _solomon_off():
        return
    if is_session_private(session):
        return
    if getattr(session, "solomon_skill_overridden", False):
        # Consume the flag so the next turn gets default treatment.
        try:
            session.solomon_skill_overridden = False
        except Exception:  # noqa: BLE001
            pass
        return

    skill = _load_default_skill()
    vocab = _load_vocabulary()
    summary, is_empty = _load_profile_summary()
    is_inbound, kind, sid, channel = is_external_inbound(messages, session)

    block_lines = [
        "# Solomon role (loaded on every turn)",
        "",
        skill,
        "",
        "# Owner vocabulary",
        "",
        vocab.strip() if vocab.strip() else "(vocabulary file is empty — capture phrases as you hear them)",
        "",
        "# Profile summary",
        "",
        summary.strip() if summary.strip() else "(profile is empty — first turn invite the owner to /onboard)",
        "",
        _tool_menu(),
    ]
    if is_empty:
        block_lines.append("\nNOTE: profile.yaml is empty. Invite the owner to run /onboard.")
    if is_inbound:
        block_lines.append(
            f"\nINBOUND CONTEXT: This message is from an external source via {channel} "
            f"(source_kind={kind!r}, source_id={sid!r}). Apply the two-pass inbound flow "
            "per your skill instructions: gut-check first, then load relevant playbooks "
            "and refine, then call propose_action or note_handled."
        )

    block = "\n".join(block_lines)
    # Prepend as a system message. Mutate the list in place so Hermes sees it.
    messages.insert(0, {"role": "system", "content": block})
    logs.log("skill_loaded", skill="solomon-default",
             session_id=getattr(session, "id", None),
             context={"inbound": is_inbound, "kind": kind})


def post_llm_call(response: Any, session: Any) -> None:
    """Log the turn and trigger pending-action notification dispatch."""
    if is_session_private(session):
        logs.log("private_turn")
        return
    tokens_in = getattr(response, "input_tokens", None) or getattr(response, "tokens_in", None)
    tokens_out = getattr(response, "output_tokens", None) or getattr(response, "tokens_out", None)
    logs.log("turn_end",
             session_id=getattr(session, "id", None),
             tokens_in=tokens_in,
             tokens_out=tokens_out)
    # Dispatch any pending notifications the LLM wrote during this turn.
    try:
        from . import inbound  # lazy import to avoid circulars during plugin load
        inbound.dispatch_pending_notifications(session=session)
    except Exception as e:  # noqa: BLE001
        logs.log_error("error", e, where="hooks.post_llm_call.dispatch")


def on_session_start(session: Any) -> None:
    try:
        session.private = False
    except Exception:  # noqa: BLE001
        pass
    logs.log("session_start", session_id=getattr(session, "id", None))


def register_all(adapter) -> None:  # noqa: ANN001
    adapter.register_hook("pre_llm_call", pre_llm_call)
    adapter.register_hook("post_llm_call", post_llm_call)
    adapter.register_hook("on_session_start", on_session_start)

"""One-file wrapper over the Hermes plugin context.

Everything Solomon does that touches Hermes goes through this adapter.
If Hermes's API ever changes shape, this is the only file that needs
to update. The rest of Solomon depends on the adapter's stable surface.

The adapter intentionally has no behavior of its own. It's a thin
mapping layer between Solomon's vocabulary and Hermes's.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from . import logs


class HermesAdapter:
    """Wraps the ctx Hermes passes to register(ctx)."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    # -- Tools -------------------------------------------------------------

    def register_tool(self, *, name: str, description: str,
                      parameters: dict, handler: Callable[[dict], Any]) -> None:
        """Register a tool. Tries the documented Hermes signature."""
        try:
            self.ctx.register_tool(
                name=name,
                description=description,
                parameters=parameters,
                handler=handler,
            )
        except TypeError:
            # Older Hermes versions used positional args; fall back.
            self.ctx.register_tool(name, description, parameters, handler)

    # -- Slash commands ----------------------------------------------------

    def register_command(self, *, name: str, description: str,
                          handler: Callable[[dict], Any]) -> None:
        try:
            self.ctx.register_command(
                name=name, description=description, handler=handler
            )
        except TypeError:
            self.ctx.register_command(name, description, handler)

    # -- Hooks -------------------------------------------------------------

    def register_hook(self, name: str, callback: Callable) -> None:
        self.ctx.register_hook(name, callback)

    # -- Config ------------------------------------------------------------

    def get_config(self, key: str, default: Any = None) -> Any:
        getter = getattr(self.ctx, "get_config", None)
        if getter is None:
            return default
        try:
            return getter(key, default)
        except TypeError:
            return getter(key) or default

    # -- Conversation history (used by daily.py) ---------------------------

    def read_recent_conversations(self, since) -> list[dict]:
        """Return Hermes conversation turns since `since` (datetime).

        Each dict has at least: session_id, turns (list of message dicts
        with role, content, ts), and a `private` flag if Hermes marks it.

        If Hermes does not expose this API, returns an empty list and logs
        a warning. The daily cron treats an empty list as "no reflection
        this run" — degraded but not broken.
        """
        getter = getattr(self.ctx, "read_recent_conversations", None)
        if getter is None:
            logs.log("hermes_api_missing", level="WARN",
                     context={"method": "read_recent_conversations"})
            return []
        try:
            return list(getter(since))
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where="adapter.read_recent_conversations")
            return []

    # -- Gateway-initiated messages (used by checkin.py and inbound.py) ----

    def send_to_owner(self, text: str, *, channel: Optional[str] = None) -> bool:
        """Push a message to the owner via Hermes's preferred-channel API.

        Returns True on success, False if Hermes is unreachable (the caller
        is responsible for queuing into pending_messages.jsonl).
        """
        sender = getattr(self.ctx, "send_to_owner", None)
        if sender is None:
            sender = getattr(self.ctx, "send_message_to_owner", None)
        if sender is None:
            logs.log("hermes_api_missing", level="WARN",
                     context={"method": "send_to_owner"})
            return False
        try:
            sender(text, channel=channel) if channel else sender(text)
            return True
        except TypeError:
            try:
                sender(text)
                return True
            except Exception as e:  # noqa: BLE001
                logs.log_error("error", e, where="adapter.send_to_owner")
                return False
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where="adapter.send_to_owner")
            return False

    # -- LLM client (for crons that need to call the LLM directly) ---------

    def llm_call(self, *, system: str, messages: list[dict],
                  json_mode: bool = False, max_tokens: int = 2048) -> str:
        """Call whatever LLM Hermes is configured to use.

        Returns the LLM's response text. If Hermes's LLM client is not
        available (e.g., during tests), raises RuntimeError.
        """
        client = getattr(self.ctx, "llm", None) or getattr(self.ctx, "llm_client", None)
        if client is None:
            raise RuntimeError("Hermes ctx has no LLM client exposed.")
        # Try several common signatures.
        for fn_name in ("call", "complete", "respond"):
            fn = getattr(client, fn_name, None)
            if fn is None:
                continue
            try:
                resp = fn(system=system, messages=messages, json_mode=json_mode,
                          max_tokens=max_tokens)
                return getattr(resp, "text", str(resp))
            except TypeError:
                continue
        raise RuntimeError("Hermes LLM client has no recognized call/complete/respond method.")

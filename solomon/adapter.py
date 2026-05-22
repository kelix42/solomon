"""The translator between Solomon and Hermes.

This is the ONLY file in Solomon that knows what Hermes looks like under the
hood. If the Hermes plugin contract ever changes shape, this file is where the
fix lives. The rest of Solomon — the conductor, the sleep cycle, the audit
gate, the autonomy ladder, all of it — never imports anything from Hermes
directly.

The contract this file depends on:

  - ``PluginContext`` (the ``ctx`` passed to ``register(ctx)``) exposes:
      * ``ctx.register_tool(...)``       -> registers a Solomon tool
      * ``ctx.register_command(...)``    -> registers a slash command
      * ``ctx.register_hook(name, fn)``  -> attaches a callback to a Hermes
                                           lifecycle hook
      * ``ctx.config`` / ``ctx.get_config(...)``  -> reads user config
      * ``ctx.logger``                   -> a Python logger we can write to

  - The Hermes hook names we use are defined in
    ``hermes_cli/plugins.py::VALID_HOOKS``. We adapt to whatever set is
    available at runtime; missing hooks degrade gracefully (e.g. if Hermes
    ever drops ``pre_gateway_dispatch``, capture falls back to ``pre_llm_call``
    only and logs a warning, but Solomon keeps running).

Tested against Hermes 0.14.x. The compatibility matrix lives in
``tests/test_adapter.py`` and is exercised in CI on every Hermes release.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

# We do NOT import hermes_* modules at the top level of this file. We are
# inside the Hermes process when register() runs, so anything we need is
# either passed to us via PluginContext or available through stdlib + our
# own dependencies. Keeping this file import-light means a missing Hermes
# symbol never crashes Solomon at import time — it surfaces as a clean
# AdapterError at register time, with a helpful message about which version
# of Hermes we expected.

logger = logging.getLogger("solomon.adapter")


class AdapterError(RuntimeError):
    """Raised when the Hermes contract Solomon depends on is missing or
    has changed shape in an incompatible way. Always carries a message that
    points the user at the version of Hermes we expected.
    """


# ---------------------------------------------------------------------------
# Hook names Solomon attaches to.
#
# Anything in REQUIRED_HOOKS must exist or Solomon refuses to start. Anything
# in OPTIONAL_HOOKS degrades gracefully (Solomon logs a warning and skips
# the feature that depended on the missing hook).
# ---------------------------------------------------------------------------

REQUIRED_HOOKS = (
    "pre_llm_call",
    "post_llm_call",
    "pre_tool_call",
    "post_tool_call",
    "on_session_start",
    "on_session_end",
)

OPTIONAL_HOOKS = (
    "pre_gateway_dispatch",   # gateway-only; missing in CLI-only installs
    "transform_llm_output",   # used for /private mode status bar marker
    "pre_approval_request",   # for audit-gate observability
    "post_approval_response",
)


# ---------------------------------------------------------------------------
# Public adapter surface
# ---------------------------------------------------------------------------

@dataclass
class HookAttachment:
    """Result of attaching a callback to a Hermes lifecycle hook.

    The conductor uses these to decide which Solomon features can run.
    `attached=False` means the hook didn't exist in this Hermes version;
    the feature that needed it has been disabled. `attached=True` means
    the callback is live and Hermes will invoke it.
    """
    name: str
    attached: bool
    reason: Optional[str] = None  # filled in when attached=False


class HermesAdapter:
    """Wraps the Hermes ``PluginContext`` and exposes a stable Solomon-facing
    API. Every other Solomon module gets a reference to this adapter and
    talks to it. They never see ``ctx`` directly.

    Why a class and not a module of free functions: we want one place that
    holds a reference to ``ctx``, the Hermes logger, and the lazy import
    handles. A class makes that ownership obvious and lets us swap in a
    fake adapter in tests without monkey-patching.
    """

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._attached_hooks: Dict[str, HookAttachment] = {}
        self._registered_tools: List[str] = []
        self._registered_commands: List[str] = []

        # Sanity: ensure the methods we depend on exist on ctx. If not,
        # raise immediately with a clear message instead of getting a
        # confusing AttributeError four call sites later.
        self._verify_contract()

    def _verify_contract(self) -> None:
        required = ("register_tool", "register_command", "register_hook")
        missing = [m for m in required if not hasattr(self._ctx, m)]
        if missing:
            raise AdapterError(
                "Hermes plugin context is missing required methods: "
                f"{missing}. Solomon expects Hermes >=0.14 with the "
                "PluginContext.register_tool / register_command / register_hook "
                "surface. If you are running a newer Hermes that renamed these, "
                "update solomon/adapter.py — this is the only file that needs "
                "to know."
            )

    # -- hook attachment ----------------------------------------------------

    def attach_hook(self, hook_name: str, callback: Callable[..., Any]) -> HookAttachment:
        """Attach a callback to a Hermes lifecycle hook.

        Returns a HookAttachment describing whether the attachment succeeded.
        Caller is responsible for checking ``attached`` if the feature is
        non-essential.
        """
        try:
            self._ctx.register_hook(hook_name, callback)
            att = HookAttachment(name=hook_name, attached=True)
            self._attached_hooks[hook_name] = att
            return att
        except Exception as e:  # noqa: BLE001  (we genuinely want to catch all)
            att = HookAttachment(name=hook_name, attached=False, reason=str(e))
            self._attached_hooks[hook_name] = att
            if hook_name in REQUIRED_HOOKS:
                raise AdapterError(
                    f"Required Hermes hook '{hook_name}' could not be attached: {e}"
                ) from e
            logger.warning(
                "Optional Hermes hook '%s' could not be attached: %s. "
                "Continuing without it; the dependent feature will be disabled.",
                hook_name, e,
            )
            return att

    def attach_all(self, callbacks: Dict[str, Callable[..., Any]]) -> Dict[str, HookAttachment]:
        """Bulk attach a mapping of hook_name -> callback."""
        return {name: self.attach_hook(name, cb) for name, cb in callbacks.items()}

    # -- tool registration --------------------------------------------------

    def register_tool(
        self,
        *,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Callable[..., Any],
        toolset: str = "solomon",
        check_fn: Optional[Callable[[], bool]] = None,
        requires_env: Optional[List[str]] = None,
    ) -> None:
        """Register a Solomon tool. Delegates to Hermes ``ctx.register_tool``.

        Solomon's tools (audit_gate, log_decision, store_prediction, etc.)
        all flow through here. They appear in Hermes's tool list under the
        ``solomon`` toolset and can be enabled/disabled like any other.
        """
        schema = {
            "name": name,
            "description": description,
            "parameters": parameters,
        }
        self._ctx.register_tool(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=lambda args, **kw: handler(args, **kw),
            check_fn=check_fn,
            requires_env=requires_env or [],
        )
        self._registered_tools.append(name)
        logger.debug("Registered Solomon tool: %s", name)

    # -- slash command registration -----------------------------------------

    def register_command(
        self,
        *,
        name: str,
        aliases: Optional[List[str]] = None,
        description: str,
        handler: Callable[..., Any],
    ) -> None:
        """Register a slash command (e.g. /private). Delegates to
        ``ctx.register_command``.
        """
        try:
            self._ctx.register_command(
                name=name,
                aliases=aliases or [],
                description=description,
                handler=handler,
            )
            self._registered_commands.append(name)
            logger.debug("Registered Solomon slash command: /%s", name)
        except Exception as e:  # noqa: BLE001
            raise AdapterError(
                f"Failed to register Solomon slash command '/{name}': {e}. "
                "Solomon expects the Hermes PluginContext.register_command API. "
                "If Hermes renamed this method, update solomon/adapter.py."
            ) from e

    # -- config / logging passthroughs --------------------------------------

    def get_config(self, key: str, default: Any = None) -> Any:
        """Read a config value, falling back to default. The key path uses
        dot notation as in Hermes config (e.g. 'solomon.observe_mode_days').
        """
        getter = getattr(self._ctx, "get_config", None)
        if getter is None:
            return default
        try:
            return getter(key, default)
        except Exception:  # noqa: BLE001
            return default

    def hermes_logger(self) -> logging.Logger:
        """Return the logger we should write Solomon log lines to. Defaults
        to our own if ctx doesn't expose one.
        """
        return getattr(self._ctx, "logger", logger)

    # -- introspection ------------------------------------------------------

    def hook_status(self) -> Dict[str, HookAttachment]:
        """Snapshot of which hooks attached cleanly. Used by the conductor
        at startup to log a human-readable summary of which features are
        live.
        """
        return dict(self._attached_hooks)

    def is_feature_available(self, *required_hooks: str) -> bool:
        """Return True only if every named hook is attached. Used by feature
        code that needs to decide whether to run.
        """
        return all(
            self._attached_hooks.get(h, HookAttachment(h, False)).attached
            for h in required_hooks
        )

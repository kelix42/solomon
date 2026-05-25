"""Hermes plugin entry point.

Hermes calls `register(ctx)` once at startup. We wrap ctx in an adapter,
wire up our tools/commands/hooks, and log we're ready.
"""

from __future__ import annotations

from . import logs, tools, slash, hooks
from .adapter import HermesAdapter


def register(ctx) -> HermesAdapter:  # noqa: ANN001  (Hermes passes its own ctx)
    """Hermes entry point. Called once at startup."""
    logs.setup_logging()
    adapter = HermesAdapter(ctx)
    try:
        # Make sure ~/.hermes/solomon/ exists before any tool fires.
        from . import profile
        profile.init_solomon_home()

        tools.register_all(adapter)
        slash.register_all(adapter)
        hooks.register_all(adapter)
        logs.log("solomon_ready")
    except Exception as e:  # noqa: BLE001
        logs.log_error("error", e, where="plugin.register")
        raise
    return adapter

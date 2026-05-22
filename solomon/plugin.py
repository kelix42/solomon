"""Plugin entry point. Hermes calls ``register(ctx)`` on this module when
Solomon is discovered (either as a bundled plugin, a user plugin in
~/.hermes/plugins/solomon/, or via the ``hermes_agent.plugins`` pip
entry point).

The job here is to:

1. Wrap the Hermes ctx in a Solomon adapter (the translator).
2. Initialize the conductor (the brain that wraps every Hermes turn).
3. Register Solomon's own tools (audit_gate, log_decision, etc.).
4. Register slash commands (/private).
5. Attach to Hermes lifecycle hooks (pre_llm_call, post_tool_call, ...).
6. Log a startup summary so the user can see which features are live.

This is intentionally short. All the real logic lives in the modules
this file imports. If anything here gets longer than ~150 lines, refactor.
"""

from __future__ import annotations

import logging

from .adapter import HermesAdapter, AdapterError
from .conductor import Conductor
from .private.mode import PrivateMode
from .storage.pool import get_pool, init_storage

logger = logging.getLogger("solomon")


def register(ctx):  # noqa: ANN001  (Hermes passes its own ctx type)
    """Hermes plugin entry point.

    Called once per Hermes process at startup. After this returns, Solomon
    is live and every Hermes session flows through the conductor.
    """
    try:
        adapter = HermesAdapter(ctx)
    except AdapterError as e:
        logger.error("Solomon failed to attach to Hermes: %s", e)
        # Don't crash Hermes. Surface the problem and bow out cleanly.
        return

    # Initialize storage (Postgres connection pool, schema sanity check).
    # If storage isn't reachable, Solomon refuses to start — running without
    # the decision log would silently lose data, which is worse than not
    # running.
    try:
        init_storage(adapter)
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Solomon storage initialization failed: %s. "
            "Run `solomon init` to provision the database, then restart Hermes.",
            e,
        )
        return

    # Build the components.
    private_mode = PrivateMode(adapter)
    conductor = Conductor(adapter=adapter, private_mode=private_mode)

    # Wire Solomon's tools into Hermes's tool registry.
    conductor.register_tools()

    # Wire the /private command.
    private_mode.register_command()

    # Attach to lifecycle hooks.
    conductor.attach_hooks()

    # Log a startup summary.
    status = adapter.hook_status()
    live = [name for name, a in status.items() if a.attached]
    missing = [name for name, a in status.items() if not a.attached]
    logger.info(
        "Solomon ready. Hooks live: %s. Hooks unavailable: %s.",
        ", ".join(live) or "(none)",
        ", ".join(missing) or "(none)",
    )

"""Solomon — an AI chief of staff that turns Hermes into a domain-specific
decision engine.

By the time this package is imported, Hermes has already discovered us via
either the bundled-plugin path, the user-plugin path, or our pip entry point
``hermes_agent.plugins.solomon``. ``solomon.plugin.register(ctx)`` is the
single entry point Hermes invokes; everything Solomon does to Hermes flows
through there.

Versioning: Solomon follows semver. Breaking changes to the plugin contract
require a major version bump. The Hermes plugin hook contract is treated as
load-bearing — if it changes, only ``solomon/adapter.py`` needs to know.
"""

__version__ = "0.1.0"

# Re-exports for convenience in tests and downstream code.
from . import adapter, conductor  # noqa: F401

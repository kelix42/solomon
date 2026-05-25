"""Tests for the plugin.register(ctx) entry point.

Adapter unit tests live in tests/test_adapter.py.
"""

from __future__ import annotations

from pathlib import Path

from solomon import plugin


class FakeCtx:
    """Minimal Hermes ctx — matches the real Hermes signatures."""

    def __init__(self):
        self.tools: list[dict] = []
        self.commands: list[dict] = []
        self.hooks: list[tuple] = []

    def register_tool(self, *, name, toolset, schema, handler,
                       description="", emoji="", check_fn=None,
                       requires_env=None, is_async=False, override=False):
        self.tools.append({"name": name, "toolset": toolset, "schema": schema,
                            "handler": handler})

    def register_command(self, *, name, handler, description="", args_hint=""):
        self.commands.append({"name": name, "handler": handler})

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def get_config(self, key, default=None):
        return default


def test_plugin_register_wires_all_tools(solomon_home: Path):
    ctx = FakeCtx()
    plugin.register(ctx)
    names = {t["name"] for t in ctx.tools}
    expected = {
        "read_profile", "read_playbook", "read_queue",
        "propose_addition", "flag_contradiction",
        "propose_action", "note_handled",
        "apply_queue_decision", "mark_session_complete",
    }
    # Tools must be a superset of the v1 surface. Step 4 will add more.
    assert expected.issubset(names)


def test_plugin_register_creates_home(solomon_home: Path):
    plugin.register(FakeCtx())
    assert (solomon_home / "profile.yaml").exists()


def test_plugin_register_uses_solomon_toolset(solomon_home: Path):
    ctx = FakeCtx()
    plugin.register(ctx)
    # Every registered tool should use the Solomon toolset.
    assert all(t["toolset"] == "solomon" for t in ctx.tools)

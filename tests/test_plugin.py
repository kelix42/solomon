"""Tests for the adapter and plugin entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from solomon import plugin
from solomon.adapter import HermesAdapter


class FakeCtx:
    """Minimal Hermes ctx for tests."""

    def __init__(self):
        self.tools: list[tuple] = []
        self.commands: list[tuple] = []
        self.hooks: list[tuple] = []

    def register_tool(self, *, name, description, parameters, handler):
        self.tools.append((name, description, parameters, handler))

    def register_command(self, *, name, description, handler):
        self.commands.append((name, description, handler))

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def get_config(self, key, default=None):
        return default


def test_adapter_register_tool_passes_to_ctx():
    ctx = FakeCtx()
    a = HermesAdapter(ctx)
    a.register_tool(name="x", description="d", parameters={}, handler=lambda d: None)
    assert ctx.tools[0][0] == "x"


def test_adapter_handles_old_positional_register_tool():
    """If Hermes uses positional args, fall back gracefully."""
    class OldCtx:
        def __init__(self):
            self.calls = []

        def register_tool(self, name, description, parameters, handler):
            self.calls.append((name, description, parameters, handler))

    a = HermesAdapter(OldCtx())
    a.register_tool(name="x", description="d", parameters={}, handler=lambda d: None)
    assert a.ctx.calls[0][0] == "x"


def test_adapter_send_to_owner_falls_back_gracefully():
    ctx = FakeCtx()  # has no send_to_owner method
    a = HermesAdapter(ctx)
    assert a.send_to_owner("hi") is False


def test_adapter_read_recent_conversations_returns_empty_when_missing():
    a = HermesAdapter(FakeCtx())
    assert a.read_recent_conversations(None) == []


def test_plugin_register_wires_all_tools(solomon_home: Path):
    ctx = FakeCtx()
    plugin.register(ctx)
    tool_names = [t[0] for t in ctx.tools]
    assert set(tool_names) == {
        "read_profile", "read_playbook", "read_queue",
        "propose_addition", "flag_contradiction",
        "propose_action", "note_handled",
        "apply_queue_decision", "mark_session_complete",
    }


def test_plugin_register_creates_home(solomon_home: Path):
    ctx = FakeCtx()
    plugin.register(ctx)
    assert (solomon_home / "profile.yaml").exists()

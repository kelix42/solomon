"""Tests for the Solomon ↔ Hermes adapter (solomon/adapter.py).

This is the load-bearing future-proofing file. If Hermes ever changes its
plugin contract, this test suite is what tells us — before users do —
whether the adapter still translates correctly.

The tests use a FakeCtx that imitates a Hermes PluginContext. When the
real Hermes plugin API changes shape, we update the adapter, then update
the FakeCtx here to match the new contract. The conductor, the sleep
cycle, all the other modules need NO changes — that's the whole point.
"""

from __future__ import annotations

from typing import Any, Dict, List
import pytest

from solomon.adapter import HermesAdapter, AdapterError, HookAttachment


class FakeCtx:
    """Minimal stand-in for Hermes ``PluginContext``.

    Tracks every register call and every attached hook so the tests can
    assert that the adapter is calling Hermes the way we expect.
    """

    def __init__(self) -> None:
        self.tools: List[Dict[str, Any]] = []
        self.commands: List[Dict[str, Any]] = []
        self.hooks: Dict[str, List[Any]] = {}
        # Tunable: which hooks to refuse to attach. Use to simulate a
        # Hermes version that dropped a hook.
        self.disabled_hooks: set[str] = set()
        # Tunable: pretend a method is missing
        self.disabled_methods: set[str] = set()
        self.logger = None

    def register_tool(self, **kwargs: Any) -> None:
        if "register_tool" in self.disabled_methods:
            raise AttributeError("register_tool not available")
        self.tools.append(kwargs)

    def register_command(self, **kwargs: Any) -> None:
        if "register_command" in self.disabled_methods:
            raise AttributeError("register_command not available")
        self.commands.append(kwargs)

    def register_hook(self, name: str, callback: Any) -> None:
        if "register_hook" in self.disabled_methods:
            raise AttributeError("register_hook not available")
        if name in self.disabled_hooks:
            raise ValueError(f"Hook '{name}' not supported in this Hermes version")
        self.hooks.setdefault(name, []).append(callback)

    def get_config(self, key: str, default: Any = None) -> Any:
        return default


def test_adapter_init_succeeds_with_full_contract():
    ctx = FakeCtx()
    HermesAdapter(ctx)  # should not raise


def test_adapter_init_rejects_missing_register_tool():
    class Minimal:
        def register_command(self, **kwargs): pass
        def register_hook(self, name, cb): pass
        # no register_tool
    with pytest.raises(AdapterError, match="missing required methods"):
        HermesAdapter(Minimal())


def test_attach_required_hook_records_attachment():
    ctx = FakeCtx()
    a = HermesAdapter(ctx)
    att = a.attach_hook("pre_llm_call", lambda **kw: None)
    assert att.attached is True
    assert att.name == "pre_llm_call"
    assert "pre_llm_call" in ctx.hooks


def test_attach_required_hook_raises_when_unavailable():
    ctx = FakeCtx()
    ctx.disabled_hooks = {"pre_llm_call"}
    a = HermesAdapter(ctx)
    with pytest.raises(AdapterError, match="pre_llm_call"):
        a.attach_hook("pre_llm_call", lambda **kw: None)


def test_attach_optional_hook_degrades_gracefully():
    ctx = FakeCtx()
    ctx.disabled_hooks = {"pre_gateway_dispatch"}
    a = HermesAdapter(ctx)
    att = a.attach_hook("pre_gateway_dispatch", lambda **kw: None)
    assert att.attached is False
    assert att.reason is not None


def test_register_tool_passes_schema_to_hermes():
    ctx = FakeCtx()
    a = HermesAdapter(ctx)
    a.register_tool(
        name="my_tool",
        description="does a thing",
        parameters={"type": "object", "properties": {}},
        handler=lambda args, **kw: "ok",
    )
    assert len(ctx.tools) == 1
    assert ctx.tools[0]["name"] == "my_tool"
    assert ctx.tools[0]["schema"]["description"] == "does a thing"
    assert ctx.tools[0]["toolset"] == "solomon"


def test_register_command_registers_alias_as_separate_command():
    """Hermes' PluginContext.register_command has no native aliases support.

    The adapter works around this by registering each alias as its own
    slash command pointing at the same handler.
    """
    ctx = FakeCtx()
    a = HermesAdapter(ctx)
    handler = lambda **kw: "ok"  # noqa: E731
    a.register_command(
        name="private",
        aliases=["priv"],
        description="toggle private mode",
        handler=handler,
    )
    assert len(ctx.commands) == 2
    names = sorted(c["name"] for c in ctx.commands)
    assert names == ["priv", "private"]
    # Both share the same handler.
    for cmd in ctx.commands:
        assert cmd["handler"] is handler
        assert cmd["description"] == "toggle private mode"


def test_is_feature_available_returns_false_when_any_hook_missing():
    ctx = FakeCtx()
    ctx.disabled_hooks = {"pre_gateway_dispatch"}
    a = HermesAdapter(ctx)
    a.attach_hook("pre_llm_call", lambda **kw: None)
    a.attach_hook("pre_gateway_dispatch", lambda **kw: None)
    assert a.is_feature_available("pre_llm_call") is True
    assert a.is_feature_available("pre_llm_call", "pre_gateway_dispatch") is False


def test_attach_all_returns_status_per_hook():
    ctx = FakeCtx()
    # post_llm_call is REQUIRED, so we can't disable it without raising.
    # Use two optional hooks here.
    ctx.disabled_hooks = {"pre_gateway_dispatch"}
    a = HermesAdapter(ctx)
    results = a.attach_all({
        "pre_gateway_dispatch": lambda **kw: None,
        "transform_llm_output": lambda **kw: None,
    })
    assert results["pre_gateway_dispatch"].attached is False
    assert results["transform_llm_output"].attached is True


def test_register_command_raises_with_helpful_message_when_method_missing():
    class Minimal:
        def register_tool(self, **kw): pass
        def register_hook(self, name, cb): pass
        # no register_command
    with pytest.raises(AdapterError, match="missing required methods"):
        HermesAdapter(Minimal())

"""Integration test through a faithful mock of Hermes's registry.dispatch.

This sits a layer above ``test_tools.py``. The point isn't to exercise each
tool's behavior — those tests do that already. The point is to exercise the
dispatch *path* that bit us twice:

1. 2026-05-25 incident #1: Hermes's ``tools/registry.py:404`` calls
   ``handler(args, **kwargs)`` (passing ``task_id`` and friends). Our
   wrapper only accepted ``handler(args)`` so every tool call raised
   ``TypeError`` in production while every unit test passed.

2. 2026-05-25 incident #2 (averted): some LLM providers stringify
   numerics. ``mark_session_complete({"session_n": "0", ...})`` would
   silently ``ValueError`` (str ``"0"`` not in the int-keyed
   ``SESSION_SECTION``) and the LLM would then narrate "session
   locked in" anyway.

3. 2026-05-29 incident #3: Hermes's ``agent/tool_executor.py`` calls
   ``len(result)`` on every handler return, so a non-string return
   (e.g. ``mark_session_complete`` returns ``True``) raised
   ``TypeError: object of type 'bool' has no len()``. Our wrapper now
   JSON-encodes any non-string return, so dispatch always yields a
   string (``True`` -> ``"true"``, a list -> its JSON text).

The mock here mirrors the real Hermes shape:

    class Registry:
        def dispatch(self, name, args, **kwargs):
            return entry.handler(args, **kwargs)

Anything that breaks on the real ``Registry`` should break here too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest


class FakeRegistry:
    """Minimal stand-in for Hermes's ``tools.registry.Registry``.

    Mirrors the dispatch signature at ``hermes-agent/tools/registry.py:390``
    so any change to that contract breaks here in CI rather than on a
    user's live install.
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}

    def register_tool(self, *, name: str, description: str, schema: dict,
                       handler: Callable) -> None:
        # Same shape as solomon.adapter.HermesAdapter.register_tool's
        # final outbound call (which is what Hermes itself receives).
        self._entries[name] = {
            "handler": handler,
            "schema": schema,
            "description": description,
        }

    # Stubs for the adapter-side methods that cron-side tools call through
    # tools._adapter. The integration test isn't asserting their behavior —
    # just that the dispatch + coercion path doesn't blow up before reaching
    # them.
    def read_conversations(self, **_):
        return []

    def send_to_owner(self, text: str, target=None) -> bool:
        return True

    def dispatch(self, name: str, args: dict, **kwargs: Any) -> Any:
        """Match Hermes 0.14 dispatch shape exactly."""
        entry = self._entries.get(name)
        if not entry:
            raise LookupError(f"unknown tool {name!r}")
        return entry["handler"](args, **kwargs)

    def get_definitions(self, tool_names: set) -> list[dict]:
        """Mirrors Hermes's tools/registry.py:get_definitions (line 366-383).

        This is the path that builds the JSON the LLM actually sees. If our
        schemas aren't in OpenAI function-spec shape, the LLM gets
        empty-parameters tool stubs and either ignores them or falls back
        to execute_code. The 2026-05-26 incident was caused by the LLM
        seeing this output for mark_session_complete:

            {"type": "function", "function": {
                "type": "object", "properties": {...},
                "required": [...], "name": "mark_session_complete"}}

        instead of:

            {"type": "function", "function": {
                "name": "mark_session_complete",
                "description": "...",
                "parameters": {"type": "object", "properties": {...}}}}
        """
        result = []
        for name in sorted(tool_names):
            entry = self._entries.get(name)
            if not entry:
                continue
            schema_with_name = {**entry["schema"], "name": name}
            result.append({"type": "function", "function": schema_with_name})
        return result


@pytest.fixture
def registry(solomon_home: Path) -> FakeRegistry:
    """A FakeRegistry with every Solomon tool wired through register_all,
    same as live Hermes. The solomon_home fixture initializes profile.yaml
    so the file-touching tools work."""
    from solomon import profile, tools
    profile.init_solomon_home()
    r = FakeRegistry()
    tools.register_all(r)
    return r


# ---------------------------------------------------------------------------
# Kwargs acceptance — regression for the task_id TypeError
# ---------------------------------------------------------------------------


def test_dispatch_passes_task_id_to_every_tool(registry: FakeRegistry):
    """Hermes passes task_id (and may pass others) on every dispatch.
    None of our 19 tools should TypeError on it."""
    # read_profile has no side effects and minimal args — good canary.
    result = registry.dispatch(
        "read_profile",
        {"section": "industry"},
        task_id="20260525_181951_2bb658",
    )
    assert "not yet filled" in result


def test_dispatch_passes_unexpected_future_kwargs(registry: FakeRegistry):
    """If Hermes adds new context kwargs (session_id, model, etc.) we
    must not regress to the August handler-shape bug."""
    out = registry.dispatch(
        "read_profile",
        {"section": "industry"},
        task_id="x",
        session_id="y",
        model="z",
        platform="cli",
    )
    assert "not yet filled" in out


# ---------------------------------------------------------------------------
# Type coercion — for every arg that isn't a plain string
# ---------------------------------------------------------------------------


def test_mark_session_complete_with_stringified_session_n(registry: FakeRegistry):
    """LLM passes "0" instead of 0. Must coerce, not silently fail."""
    summary = {
        "business_category": "consulting",
        "primary_product_or_service": "advisory",
        "customer_orientation": "B2B",
        "geographic_scope": "national",
        "revenue_model": "project",
        "growth_stage": "early",
        "concentration_risk": "two large clients",
    }
    result = registry.dispatch(
        "mark_session_complete",
        {"session_n": "0", "summary": summary},
        task_id="t",
    )
    # Dispatch returns a JSON string (the wrapper encodes the bool).
    assert result == "true"
    # Verify the write actually landed.
    from solomon import profile
    import yaml
    data = yaml.safe_load((profile.home() / "profile.yaml").read_text())
    assert data["industry"]["filled"] is True
    assert data["industry"]["business_category"] == "consulting"


def test_mark_session_complete_with_jsonstring_summary(registry: FakeRegistry):
    """LLM serializes the summary dict to a JSON string instead of passing
    a real object. Coerce."""
    import json
    summary_str = json.dumps({
        "business_category": "consulting",
        "primary_product_or_service": "advisory",
        "customer_orientation": "B2B",
        "geographic_scope": "national",
        "revenue_model": "project",
        "growth_stage": "early",
        "concentration_risk": "two large clients",
    })
    result = registry.dispatch(
        "mark_session_complete",
        {"session_n": 0, "summary": summary_str},
        task_id="t",
    )
    assert result == "true"


def test_mark_session_complete_rejects_garbage_session_n(registry: FakeRegistry):
    """Coercion shouldn't be a free pass for arbitrary inputs."""
    with pytest.raises((ValueError, TypeError)):
        registry.dispatch(
            "mark_session_complete",
            {"session_n": "banana", "summary": {}},
            task_id="t",
        )


def test_read_queue_with_stringified_limit(registry: FakeRegistry):
    import json
    out = registry.dispatch(
        "read_queue",
        {"status": "pending", "limit": "5"},
        task_id="t",
    )
    # Dispatch JSON-encodes the list return; decode to assert its shape.
    assert isinstance(out, str)
    assert isinstance(json.loads(out), list)


def test_read_conversations_with_stringified_ints_and_bool(registry: FakeRegistry):
    """All three of (since_hours, limit, exclude_private) can arrive
    stringified depending on provider."""
    import json
    out = registry.dispatch(
        "read_conversations",
        {"since_hours": "24", "limit": "50", "exclude_private": "true"},
        task_id="t",
    )
    assert isinstance(out, str)
    assert isinstance(json.loads(out), list)


def test_read_inbox_file_with_stringified_max_chars(solomon_home: Path,
                                                      registry: FakeRegistry):
    inbox = solomon_home / "inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "x.txt").write_text("hello there")
    out = registry.dispatch(
        "read_inbox_file",
        {"name": "x.txt", "max_chars": "100"},
        task_id="t",
    )
    assert "hello there" in out


def test_flag_contradiction_with_jsonstring_sources(registry: FakeRegistry):
    """LLM serializes the list of sources into a JSON string."""
    out = registry.dispatch(
        "flag_contradiction",
        {"description": "two contradictory rules",
         "sources": '["finance.md#pricing", "sales.md#discounts"]'},
        task_id="t",
    )
    assert out.startswith("q_")


def test_propose_action_with_jsonstring_payload(registry: FakeRegistry):
    """action_payload might come back JSON-string-encoded."""
    out = registry.dispatch(
        "propose_action",
        {
            "source_kind": "email",
            "source_id": "msg-1",
            "source_summary": "vendor pricing increase",
            "first_pass_prediction": "ack and review at next budget cycle",
            "final_recommendation": "request a meeting before accepting",
            "reasoning": "vendors.md says > 10% warrants renegotiation",
            "urgency": "medium",
            "action_kind": "draft_reply",
            "action_payload": '{"to": "vendor@example.com", "body": "let us meet"}',
        },
        task_id="t",
    )
    assert out.startswith("a_")


# ---------------------------------------------------------------------------
# Tool advertisement — the LLM reads this BEFORE calling. If parameters
# is missing or empty, dispatch never gets exercised in real use.
# ---------------------------------------------------------------------------


def test_get_definitions_emits_openai_function_spec(registry: FakeRegistry):
    """Regression for 2026-05-26: the LLM saw mark_session_complete as a
    parameterless tool stub because Solomon's schema was raw JSON Schema
    (type/properties at the top), not wrapped under `parameters`.

    Hermes does `{**entry.schema, "name": entry.name}` (registry.py:366) —
    so whatever we hand to register_tool's `schema=` kwarg becomes the
    LLM-visible function object verbatim. The contract is OpenAI tool
    spec: function must have `name`, `description`, `parameters` (and
    parameters must be a JSON Schema object).
    """
    defs = registry.get_definitions({
        "mark_session_complete", "read_profile", "propose_addition",
        "apply_queue_decision", "propose_action",
    })
    by_name = {d["function"]["name"]: d["function"] for d in defs}

    for tool_name, fn in by_name.items():
        assert fn.get("description"), (
            f"{tool_name}: LLM-visible function is missing 'description'"
        )
        assert "parameters" in fn, (
            f"{tool_name}: LLM-visible function is missing 'parameters' — "
            "this is the bug that bit on 2026-05-26"
        )
        params = fn["parameters"]
        assert params.get("type") == "object"
        assert "properties" in params

    # mark_session_complete must specifically advertise session_n + summary
    # — the LLM has to see these to call the tool with real args.
    msc = by_name["mark_session_complete"]
    assert "session_n" in msc["parameters"]["properties"]
    assert "summary" in msc["parameters"]["properties"]
    assert "session_n" in msc["parameters"].get("required", [])
    assert "summary" in msc["parameters"].get("required", [])


def test_get_definitions_for_every_tool_has_parameters(registry: FakeRegistry):
    """Pin the contract for every tool, not just a sample."""
    all_names = {
        "read_profile", "read_playbook", "read_queue", "read_conversations",
        "propose_addition", "flag_contradiction",
        "propose_action", "note_handled",
        "propose_compression", "apply_queue_decision",
        "apply_profile_summary", "mark_session_complete",
        "list_inbox", "read_inbox_file", "archive_file",
        "list_pending_actions_due_for_nudge", "send_nudge", "send_to_owner",
        "retry_pending_messages",
    }
    defs = registry.get_definitions(all_names)
    assert len(defs) == 19
    for d in defs:
        fn = d["function"]
        name = fn["name"]
        assert "parameters" in fn, f"{name}: no parameters key"
        assert fn["parameters"].get("type") == "object", (
            f"{name}: parameters.type must be 'object'"
        )
        # The wrapper from register_all should always set description.
        assert fn.get("description"), f"{name}: no description"


# ---------------------------------------------------------------------------
# Sanity check — the path remains end-to-end with realistic args
# ---------------------------------------------------------------------------


def test_full_session_save_end_to_end(registry: FakeRegistry):
    """Reproduce the exact path that failed on 2026-05-25 during the live
    interview: LLM calls mark_session_complete followed by
    apply_profile_summary. Both must succeed when dispatched the way
    Hermes dispatches them."""
    summary = {
        "business_category": "boutique consultancy",
        "primary_product_or_service": "strategy advisory",
        "customer_orientation": "B2B",
        "geographic_scope": "national",
        "revenue_model": "project-based",
        "growth_stage": "established",
        "concentration_risk": "no single client > 20%",
    }

    rc1 = registry.dispatch(
        "mark_session_complete",
        {"session_n": 0, "summary": summary},
        task_id="20260525_181951_2bb658",
    )
    assert rc1 == "true"

    rc2 = registry.dispatch(
        "apply_profile_summary",
        {"text": "Boutique B2B strategy consultancy; project model; no concentration risk."},
        task_id="20260525_181951_2bb658",
    )
    assert rc2 == "true"

    # Confirm both writes hit disk.
    from solomon import profile
    import yaml
    data = yaml.safe_load((profile.home() / "profile.yaml").read_text())
    assert data["industry"]["filled"] is True
    assert data["industry"]["business_category"] == "boutique consultancy"
    assert "boutique" in (data.get("summary") or {}).get("text", "").lower()

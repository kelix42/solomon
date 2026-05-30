"""Tests for the nine tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from solomon import profile, tools


def test_read_profile_unknown_section(solomon_home: Path):
    profile.init_solomon_home()
    with pytest.raises(ValueError):
        tools.read_profile("nonsense")


def test_read_playbook_unknown(solomon_home: Path):
    profile.init_solomon_home()
    with pytest.raises(ValueError):
        tools.read_playbook("not_a_playbook")


def test_propose_addition_bad_file_rejected(solomon_home: Path):
    profile.init_solomon_home()
    with pytest.raises(ValueError):
        tools.propose_addition(file="customers.md", section="x", content="y", reason="z")


def test_propose_addition_with_correct_file_name(solomon_home: Path):
    profile.init_solomon_home()
    iid = tools.propose_addition(
        file="customers", section="Contacts", content="bob@example.com is the rep",
        reason="from a conversation",
    )
    items = tools.read_queue(status="pending")
    assert any(it["id"] == iid for it in items)
    assert "[EMAIL]" in items[0]["content"]


def test_flag_contradiction_requires_two_sources(solomon_home: Path):
    profile.init_solomon_home()
    with pytest.raises(ValueError):
        tools.flag_contradiction(description="x", sources=["only_one.md"])


def test_propose_action_writes_to_action_queue(solomon_home: Path):
    profile.init_solomon_home()
    iid = tools.propose_action(
        source_kind="email",
        source_id="<msg-001@example.com>",
        source_summary="Vendor pricing question",
        first_pass_prediction="acknowledge and review",
        final_recommendation="reply requesting a call",
        reasoning="vendors.md says always negotiate >10% increases",
        urgency="medium",
        action_kind="draft_reply",
        action_payload={"to": "vendor@example.com", "body": "..."},
        playbooks_consulted=["vendors", "finance"],
    )
    assert iid.startswith("a_")
    items = tools.read_queue(status="pending", queue="actions")
    assert any(it["id"] == iid for it in items)


def test_propose_action_invalid_urgency(solomon_home: Path):
    profile.init_solomon_home()
    with pytest.raises(ValueError):
        tools.propose_action(
            source_kind="email", source_id="x", source_summary="x",
            first_pass_prediction="x", final_recommendation="x",
            reasoning="x", urgency="urgent", action_kind="draft_reply",
        )


def test_propose_action_dedupes_same_source(solomon_home: Path):
    profile.init_solomon_home()
    iid1 = tools.propose_action(
        source_kind="email", source_id="<m-1>", source_summary="x",
        first_pass_prediction="a", final_recommendation="a",
        reasoning="r", urgency="low", action_kind="record_only",
    )
    iid2 = tools.propose_action(
        source_kind="email", source_id="<m-1>", source_summary="x",
        first_pass_prediction="b", final_recommendation="b",
        reasoning="r", urgency="low", action_kind="record_only",
    )
    assert iid1 == iid2
    items = tools.read_queue(status="pending", queue="actions")
    assert len(items) == 1


def test_note_handled_logs_only(solomon_home: Path):
    profile.init_solomon_home()
    assert tools.note_handled("email", "<m-2>", "newsletter") is True
    items = tools.read_queue(status="pending", queue="actions")
    assert not items


def test_apply_queue_decision_addition_approve(solomon_home: Path):
    profile.init_solomon_home()
    iid = tools.propose_addition(
        file="finance", section="Pricing", content="No discount > 15%.",
        reason="from chat",
    )
    tools.apply_queue_decision(iid, "approve")
    # The addition should now be in the playbook.
    content = tools.read_playbook("finance")
    assert "No discount > 15%." in content
    items = tools.read_queue(status="approved")
    assert any(it["id"] == iid for it in items)


def test_apply_queue_decision_addition_edit(solomon_home: Path):
    profile.init_solomon_home()
    iid = tools.propose_addition(
        file="finance", section="Pricing", content="No discount > 15%.",
        reason="from chat",
    )
    tools.apply_queue_decision(iid, "edit", edited_content="No discount over 12%.")
    content = tools.read_playbook("finance")
    assert "12%" in content
    assert "15%" not in content


def test_apply_queue_decision_addition_reject(solomon_home: Path):
    profile.init_solomon_home()
    iid = tools.propose_addition(
        file="finance", section="Pricing", content="Bad rule.",
        reason="oops",
    )
    tools.apply_queue_decision(iid, "reject")
    content = tools.read_playbook("finance")
    assert "Bad rule." not in content
    items = tools.read_queue(status="rejected")
    assert any(it["id"] == iid for it in items)


def test_apply_queue_decision_compression_archives_old(solomon_home: Path):
    profile.init_solomon_home()
    # Seed: write something into finance.md so there's content to compress.
    profile.insert_into_playbook("finance", "X", "Big verbose content. " * 5)
    # Manually queue a compression item.
    iid = profile.append_review_item({
        "kind": "compression",
        "file": "finance",
        "section": None,
        "content": "# Finance\n\nCompressed.\n\nLast updated: 2026-01-01\n\n## See also\n",
        "reason": "tightened",
    })
    tools.apply_queue_decision(iid, "approve")
    content = tools.read_playbook("finance")
    assert "Compressed." in content
    # Archive should now have the old version.
    arch_dir = solomon_home / "archive" / "compressed"
    assert arch_dir.exists() and any(arch_dir.rglob("finance*.md"))


def test_apply_queue_decision_action_approve(solomon_home: Path):
    profile.init_solomon_home()
    iid = tools.propose_action(
        source_kind="email", source_id="<m-3>", source_summary="x",
        first_pass_prediction="x", final_recommendation="x",
        reasoning="x", urgency="medium", action_kind="draft_reply",
    )
    tools.apply_queue_decision(iid, "approve")
    item = profile.find_queue_item("actions", iid)
    assert item["status"] == "approved"
    assert item["owner_decision"] == "approve"


def test_mark_session_complete_via_tool(solomon_home: Path):
    profile.init_solomon_home()
    tools.mark_session_complete(0, {
        "business_category": "law",
        "primary_product_or_service": "title work",
        "customer_orientation": "B2C",
        "geographic_scope": "local",
        "revenue_model": "project",
        "growth_stage": "established",
        "concentration_risk": "low",
    })
    import yaml
    data = yaml.safe_load((solomon_home / "profile.yaml").read_text())
    assert data["industry"]["filled"] is True


def test_register_all_calls_adapter(solomon_home: Path):
    profile.init_solomon_home()
    calls = []

    class FakeAdapter:
        def register_tool(self, *, name, description, schema, handler):
            # Pull parameter properties out of the OpenAI-spec shape — the
            # schema dict's parameters field is what the LLM ultimately
            # reads. If properties is empty, the LLM sees a parameterless
            # tool stub (the bug that bit the live install on 2026-05-25).
            calls.append((name, schema["parameters"]["properties"], handler))

    tools.register_all(FakeAdapter())
    names = [c[0] for c in calls]
    # The v3 build added cron-side tools to the original 9. We assert the
    # original 9 are still all present; the full set lives in test_tools_step4.
    base_nine = {
        "read_profile", "read_playbook", "read_queue",
        "propose_addition", "flag_contradiction",
        "propose_action", "note_handled",
        "apply_queue_decision", "mark_session_complete",
    }
    assert base_nine.issubset(set(names))
    # Handlers should accept a dict.
    rp_handler = next(c[2] for c in calls if c[0] == "read_profile")
    result = rp_handler({"section": "industry"})
    assert "not yet filled" in result


def test_every_tool_schema_is_openai_function_spec(solomon_home: Path):
    """Regression for the 2026-05-26 incident: schemas were registered as raw
    JSON Schema (type/properties at the top level) instead of the OpenAI
    function-spec shape (description + parameters). Hermes's get_definitions
    flattens whatever's in the schema onto the function field; without a
    `parameters` wrapper the LLM sees an empty parameter list and either
    silently ignores the tool or falls back to execute_code workarounds.

    This test pins the shape for every Solomon tool so the bug can't
    regress silently. If a new tool is added without the `parameters`
    wrap, this fails."""
    profile.init_solomon_home()
    schemas: dict[str, dict] = {}

    class CaptureAdapter:
        def register_tool(self, *, name, description, schema, handler):
            schemas[name] = schema

    tools.register_all(CaptureAdapter())

    assert len(schemas) == 19, f"expected 19 tools, got {len(schemas)}"
    for name, schema in schemas.items():
        # OpenAI tool spec: each function must have "description" and
        # "parameters" at the top level. "parameters" itself is the
        # JSON Schema for the args dict.
        assert "description" in schema, (
            f"{name}: schema missing top-level 'description' field"
        )
        assert "parameters" in schema, (
            f"{name}: schema missing top-level 'parameters' field — "
            "the LLM will see an empty parameter list"
        )
        params = schema["parameters"]
        assert params.get("type") == "object", (
            f"{name}: parameters.type must be 'object' (got {params.get('type')!r})"
        )
        # `properties` is allowed to be empty for parameterless tools
        # (list_inbox, list_pending_actions_due_for_nudge, retry_pending_messages).
        assert "properties" in params, (
            f"{name}: parameters.properties missing"
        )

    # Tools that take args must have non-empty properties — otherwise the
    # LLM has no signature to call against.
    parameterless = {"list_inbox", "list_pending_actions_due_for_nudge",
                     "retry_pending_messages"}
    for name, schema in schemas.items():
        if name in parameterless:
            continue
        props = schema["parameters"]["properties"]
        assert props, f"{name}: parameters.properties empty but tool takes args"


def test_handler_accepts_extra_kwargs_from_hermes(solomon_home):
    """Hermes's tools/registry.py:404 calls `handler(args, **kwargs)` — passing
    context kwargs like task_id alongside the args dict. Our handler wrapper
    must accept and discard them, otherwise every tool call from a cron or
    live agent turn fails with TypeError. Regression test for the 2026-05-25
    incident where the LLM tried to lock in session 0 and both
    mark_session_complete + apply_profile_summary failed mid-conversation."""
    from solomon import profile, tools
    profile.init_solomon_home()

    calls = []

    class FakeAdapter:
        def register_tool(self, *, name, description, schema, handler):
            calls.append((name, handler))

    tools.register_all(FakeAdapter())
    rp_handler = next(h for n, h in calls if n == "read_profile")
    # Should NOT raise even when Hermes passes task_id (and any future kwarg).
    out = rp_handler({"section": "industry"}, task_id="some-task")
    assert "not yet filled" in out


@pytest.mark.parametrize(
    "returned, expected",
    [
        (True, "true"),
        (False, "false"),
        ({"a": 1, "b": [2, 3]}, '{"a": 1, "b": [2, 3]}'),
        ([1, "two", None], '[1, "two", null]'),
        (None, "null"),
        (42, "42"),
    ],
)
def test_make_handler_json_encodes_non_string_returns(returned, expected):
    """Hermes's tool dispatch calls len() on every handler return, so a
    non-string (bool/dict/list/None) raised 'object of type X has no len()'.
    _make_handler must JSON-encode any non-string return. Regression for the
    2026-05-29 'object of type bool has no len()' incident."""
    from solomon import tools

    def fn(**_kwargs):
        return returned

    fn.__name__ = "fake_tool"
    handler = tools._make_handler(fn)
    out = handler({})
    assert isinstance(out, str)
    assert out == expected


def test_make_handler_passes_strings_through_unchanged():
    """A handler that already returns a string must be returned verbatim —
    no double JSON-encoding (which would add surrounding quotes)."""
    from solomon import tools

    def fn(**_kwargs):
        return "q_12345"

    fn.__name__ = "fake_tool"
    handler = tools._make_handler(fn)
    assert handler({}) == "q_12345"

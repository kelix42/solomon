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
    assert result is True
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
    assert result is True


def test_mark_session_complete_rejects_garbage_session_n(registry: FakeRegistry):
    """Coercion shouldn't be a free pass for arbitrary inputs."""
    with pytest.raises((ValueError, TypeError)):
        registry.dispatch(
            "mark_session_complete",
            {"session_n": "banana", "summary": {}},
            task_id="t",
        )


def test_read_queue_with_stringified_limit(registry: FakeRegistry):
    out = registry.dispatch(
        "read_queue",
        {"status": "pending", "limit": "5"},
        task_id="t",
    )
    assert isinstance(out, list)


def test_read_conversations_with_stringified_ints_and_bool(registry: FakeRegistry):
    """All three of (since_hours, limit, exclude_private) can arrive
    stringified depending on provider."""
    out = registry.dispatch(
        "read_conversations",
        {"since_hours": "24", "limit": "50", "exclude_private": "true"},
        task_id="t",
    )
    assert isinstance(out, list)


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
    assert rc1 is True

    rc2 = registry.dispatch(
        "apply_profile_summary",
        {"text": "Boutique B2B strategy consultancy; project model; no concentration risk."},
        task_id="20260525_181951_2bb658",
    )
    assert rc2 is True

    # Confirm both writes hit disk.
    from solomon import profile
    import yaml
    data = yaml.safe_load((profile.home() / "profile.yaml").read_text())
    assert data["industry"]["filled"] is True
    assert data["industry"]["business_category"] == "boutique consultancy"
    assert "boutique" in (data.get("summary") or {}).get("text", "").lower()

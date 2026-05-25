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
        def register_tool(self, *, name, description, parameters, handler):
            calls.append((name, parameters["properties"], handler))

    tools.register_all(FakeAdapter())
    names = [c[0] for c in calls]
    assert set(names) == {
        "read_profile", "read_playbook", "read_queue",
        "propose_addition", "flag_contradiction",
        "propose_action", "note_handled",
        "apply_queue_decision", "mark_session_complete",
    }
    # Handlers should accept a dict.
    rp_handler = next(c[2] for c in calls if c[0] == "read_profile")
    result = rp_handler({"section": "industry"})
    assert "not yet filled" in result

"""Tests for profile.py: init, read/write, atomic, git-tracked."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from solomon import profile


def _git_log(home: Path):
    return subprocess.run(
        ["git", "log", "--oneline"], cwd=str(home), capture_output=True, text=True
    ).stdout


def test_init_creates_full_structure(solomon_home: Path):
    profile.init_solomon_home()
    assert (solomon_home / "profile.yaml").exists()
    for name in profile.PLAYBOOKS:
        assert (solomon_home / f"{name}.md").exists()
    assert (solomon_home / "review_queue.jsonl").exists()
    assert (solomon_home / "pending_actions.jsonl").exists()
    assert (solomon_home / ".git").exists()
    assert (solomon_home / ".gitignore").exists()


def test_init_is_idempotent(solomon_home: Path):
    profile.init_solomon_home()
    first_log = _git_log(solomon_home)
    profile.init_solomon_home()
    second_log = _git_log(solomon_home)
    # No new commit should have happened on the second init.
    assert first_log == second_log


def test_read_profile_section_empty(solomon_home: Path):
    profile.init_solomon_home()
    s = profile.read_profile_section("industry")
    assert "not yet filled" in s


def test_write_session_summary_marks_filled(solomon_home: Path):
    profile.init_solomon_home()
    profile.write_session_summary(0, {
        "business_category": "real estate law",
        "primary_product_or_service": "title work and closings",
        "customer_orientation": "B2C",
        "geographic_scope": "local",
        "revenue_model": "project",
        "growth_stage": "established",
        "concentration_risk": "top 3 clients = 50%",
    })
    raw = yaml.safe_load((solomon_home / "profile.yaml").read_text())
    assert raw["industry"]["filled"] is True
    assert raw["industry"]["business_category"] == "real estate law"
    # And git has a commit for it.
    assert "completed session 0" in _git_log(solomon_home)


def test_write_session_complete_validates_required_fields(solomon_home: Path):
    profile.init_solomon_home()
    try:
        profile.write_session_summary(0, {"business_category": "x"})
    except ValueError as e:
        assert "missing fields" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_session_6_writes_preferred_channel_to_meta(solomon_home: Path):
    profile.init_solomon_home()
    profile.write_session_summary(6, {
        "list": [{"name": "customer_pricing", "autonomy": "suggest"}],
        "preferred_channel": "telegram",
    })
    data = yaml.safe_load((solomon_home / "profile.yaml").read_text())
    assert data["meta"]["preferred_channel"] == "telegram"


def test_read_playbook_returns_template(solomon_home: Path):
    profile.init_solomon_home()
    content = profile.read_playbook("finance")
    assert content.startswith("# Finance")


def test_insert_into_playbook_creates_section(solomon_home: Path):
    profile.init_solomon_home()
    profile.insert_into_playbook("finance", "Pricing discipline",
                                  "Discounts cap at 15%.")
    content = profile.read_playbook("finance")
    assert "## Pricing discipline" in content
    assert "Discounts cap at 15%." in content
    assert "Last updated: never" not in content


def test_insert_into_existing_section_appends(solomon_home: Path):
    profile.init_solomon_home()
    profile.insert_into_playbook("finance", "Pricing discipline", "Rule A.")
    profile.insert_into_playbook("finance", "Pricing discipline", "Rule B.")
    content = profile.read_playbook("finance")
    assert "Rule A." in content
    assert "Rule B." in content


def test_review_queue_append_assigns_id_and_redacts(solomon_home: Path):
    profile.init_solomon_home()
    iid = profile.append_review_item({
        "kind": "addition",
        "file": "customers.md",
        "section": "Contact",
        "content": "Contact bob@example.com if needed.",
        "reason": "from conversation",
    })
    assert iid.startswith("q_")
    items = profile.read_queue("review", status="pending")
    assert len(items) == 1
    assert "[EMAIL]" in items[0]["content"]
    assert "bob@example.com" not in items[0]["content"]


def test_action_queue_append_redacts(solomon_home: Path):
    profile.init_solomon_home()
    iid = profile.append_action_item({
        "source_kind": "email",
        "source_id": "msg-001",
        "source_summary": "Reach out at 555-123-4567.",
        "first_pass_prediction": "draft a reply",
        "final_recommendation": "draft a reply",
        "reasoning": "test",
        "urgency": "medium",
        "action_kind": "draft_reply",
        "action_payload": {},
    })
    assert iid.startswith("a_")
    items = profile.read_queue("actions", status="pending")
    assert "[PHONE]" in items[0]["source_summary"]


def test_update_queue_item_changes_status(solomon_home: Path):
    profile.init_solomon_home()
    iid = profile.append_review_item({"kind": "addition", "file": "finance.md",
                                       "section": "Test", "content": "rule",
                                       "reason": "test"})
    assert profile.update_queue_item("review", iid, {"status": "approved"})
    assert profile.find_queue_item("review", iid)["status"] == "approved"


def test_redact_ssn():
    assert profile.redact("My SSN is 123-45-6789.") == "My SSN is [SSN]."


def test_redact_email():
    assert profile.redact("Email me at foo@bar.com.") == "Email me at [EMAIL]."


def test_redact_phone_formats():
    assert "[PHONE]" in profile.redact("Call 555-123-4567.")
    assert "[PHONE]" in profile.redact("Call (555) 123-4567.")
    assert "[PHONE]" in profile.redact("Call +1 555-123-4567.")


def test_redact_credit_card_with_luhn():
    # Valid Visa test number (Luhn-valid):
    assert "[CARD]" in profile.redact("Card: 4111 1111 1111 1111.")
    # Random Luhn-invalid digits should NOT be redacted as card.
    out = profile.redact("Part number: 1234 5678 9012 3456.")
    assert "[CARD]" not in out


def test_archive_playbook_version(solomon_home: Path):
    profile.init_solomon_home()
    archived = profile.archive_playbook_version("finance")
    assert archived is not None
    assert archived.exists()
    assert archived.read_text().startswith("# Finance")


def test_onboarding_status_counts_filled_sessions(solomon_home: Path):
    profile.init_solomon_home()
    # Fresh: nothing filled.
    s = profile.onboarding_status()
    assert s["total"] == 7
    assert s["filled"] == 0
    assert s["completed"] == []
    assert len(s["remaining"]) == 7

    # Fill two sessions; counts + names update, ordered by session number.
    profile.write_session_summary(0, {
        "business_category": "real estate law",
        "primary_product_or_service": "closings",
        "customer_orientation": "B2C",
        "geographic_scope": "Manitoba",
        "revenue_model": "flat fee",
        "growth_stage": "established",
        "concentration_risk": "spread",
    })
    profile.write_session_summary(5, {"rules": ["No closing that isn't ready."]})
    s = profile.onboarding_status()
    assert s["filled"] == 2
    assert s["completed"] == ["Industry & sector", "Non-negotiables"]
    assert "Belief system" in s["remaining"]

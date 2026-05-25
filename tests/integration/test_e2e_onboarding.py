"""End-to-end: a scripted /onboard session 0 → profile.yaml filled.

Simulates what would happen if a real owner did session 0 in Hermes:
  1. Owner types /onboard. The slash handler returns text and pushes a
     pending intent.
  2. Owner's next message triggers pre_llm_call, which claims the intent
     and sets the active mode for the session.
  3. The LLM (scripted here) eventually calls mark_session_complete.
  4. profile.yaml.industry is filled.
  5. /status shows "1 of 7 complete".

This test doesn't exercise a real LLM — it exercises the wiring around the
LLM: command dispatch, pending-intent claiming, tool calls, file writes,
git commits.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

from solomon import hooks, profile, session_state, slash, tools


def test_full_session_0_flow(solomon_home: Path):
    # 1. Owner types /onboard. Handler returns text and pushes a pending intent.
    out = slash.cmd_onboard("")
    assert "session 0" in out
    assert "Industry & sector" in out

    # 2. Owner's next message in this Hermes session. pre_llm_call fires,
    #    claims the intent, sets onboarding mode for the session.
    block = hooks.pre_llm_call(
        session_id="hermes-sess-1",
        user_message="I run a small real estate law firm.",
        conversation_history=[],
        is_first_turn=True,
        model="claude-sonnet-4-6",
        platform="cli",
    )
    assert block is not None
    assert "MODE: onboarding" in block["context"]
    assert "business_category" in block["context"]

    # 3. The scripted "LLM" eventually calls mark_session_complete.
    summary_payload = {
        "business_category": "real estate law",
        "primary_product_or_service": "residential title closings and small commercial work",
        "customer_orientation": "B2C",
        "geographic_scope": "local",
        "revenue_model": "project",
        "growth_stage": "established",
        "concentration_risk": "top three clients account for roughly half of billings",
    }
    assert tools.mark_session_complete(0, summary_payload) is True

    # 4. profile.yaml is filled.
    data = yaml.safe_load((solomon_home / "profile.yaml").read_text())
    assert data["industry"]["filled"] is True
    assert data["industry"]["business_category"] == "real estate law"
    assert data["meta"]["last_updated"]

    # 5. /status shows 1 of 7 complete.
    status_text = slash.cmd_status("")
    assert "1 of 7 complete" in status_text
    assert "✓ 0" in status_text

    # 6. The next /onboard call advances to session 1.
    out2 = slash.cmd_onboard("")
    assert "session 1" in out2
    assert "Belief system" in out2
    # And the next turn's intent is for session 1.
    intent = session_state.claim_pending_intent("hermes-sess-2")
    assert intent["session_n"] == 1


def test_full_proactive_inbound_flow(solomon_home: Path):
    """An inbound email lands → propose_action → owner approves → action dispatches."""
    profile.init_solomon_home()
    # Set a preferred channel so the notifier knows where to send.
    data = yaml.safe_load((solomon_home / "profile.yaml").read_text())
    data["meta"]["preferred_channel"] = "telegram"
    (solomon_home / "profile.yaml").write_text(yaml.safe_dump(data, sort_keys=False))

    # Scripted LLM "decides" to propose an action.
    iid = tools.propose_action(
        source_kind="email",
        source_id="<msg-mckinley-001@example.com>",
        source_summary="Vendor X says court filing fees go up 12% next month.",
        first_pass_prediction="Acknowledge and review at next budget cycle.",
        final_recommendation="Reply requesting a meeting before accepting; cite history.",
        reasoning="vendors.md flags >10% increases for renegotiation.",
        urgency="medium",
        action_kind="draft_reply",
        action_payload={"to": "billing@vendorx.example.com",
                         "subject": "Re: rate change",
                         "body": "Hi — could we jump on a quick call before this rolls out?"},
        playbooks_consulted=["vendors", "finance"],
    )

    # Notify the owner.
    from solomon import inbound

    class FakeAdapter:
        def __init__(self):
            self.sent = []
            self.dispatched = []
            # ctx needs a draft-reply handler for dispatch to succeed.
            self.ctx = SimpleNamespace(
                send_reply=lambda **kw: self.dispatched.append(("send_reply", kw)),
            )

        def send_to_owner(self, text, *, channel=None):
            self.sent.append((text, channel))
            return True

    a = FakeAdapter()
    sent = inbound.dispatch_pending_notifications(adapter=a)
    assert sent == 1
    assert a.sent  # owner got the notification

    # Owner replies "approve" — Solomon parses and dispatches.
    parsed = inbound.parse_owner_decision("approve")
    assert parsed == (iid, "approve", None)
    inbound.apply_owner_decision(*parsed, adapter=a)

    final = profile.find_queue_item("actions", iid)
    assert final["status"] == "dispatched"
    assert final["owner_decision"] == "approve"


def test_full_mentoring_walks_review_queue(solomon_home: Path):
    """/mentor surfaces queue items and the LLM can act on them via apply_queue_decision."""
    profile.init_solomon_home()
    # Seed two addition proposals.
    a1 = tools.propose_addition(file="finance", section="Pricing",
                                 content="No discount over 15%.",
                                 reason="from conversation")
    a2 = tools.propose_addition(file="customers", section="Common objections",
                                 content="Always push back on price first.",
                                 reason="from email thread")
    # The owner says "approve" on both.
    tools.apply_queue_decision(a1, "approve")
    tools.apply_queue_decision(a2, "approve")
    # Now the playbooks should have the new content.
    assert "No discount over 15%." in tools.read_playbook("finance")
    assert "Always push back on price first." in tools.read_playbook("customers")
    # And the queue items are marked approved.
    approved = profile.read_queue("review", status="approved", limit=100)
    assert len(approved) == 2

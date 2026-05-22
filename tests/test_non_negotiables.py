"""Tests for the rewritten solomon.non_negotiables.check shim.

Covers:
  - hard-rule (JSON-logic) hit → returns a NonNegotiableViolation
  - clean event → returns None
  - LLM escape hatch (check_type='llm') is reachable
  - empty / missing rules file degrades to no-op
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from solomon.non_negotiables import check as check_mod
from solomon.non_negotiables.check import NonNegotiableChecker, NonNegotiableViolation


def _make_event(raw_content="say something", scope=None, source="telegram"):
    return SimpleNamespace(
        raw_content=raw_content,
        source=source,
        participants=[],
        channel_metadata={},
    )


# ---------------------------------------------------------------------------
# JSON-logic hits + misses
# ---------------------------------------------------------------------------

def test_hard_rule_hit_triggers_block(monkeypatch):
    """A matching JSON-logic rule returns a NonNegotiableViolation."""
    # Pretend the foundation file loaded these rules.
    monkeypatch.setattr(
        check_mod, "_load_rules",
        lambda: [
            {
                "id": "no-sunday-work",
                "scope": "all",
                "condition": {"==": [{"var": "event.classification.scope"}, "scheduling"]},
                "on_violate": {"explanation": "no autonomous scheduling on Sundays"},
            },
        ],
    )
    chk = NonNegotiableChecker(adapter=None)
    ev = _make_event(raw_content="schedule the meeting", scope="scheduling")

    result = chk.check(ev, scope="scheduling")
    assert isinstance(result, NonNegotiableViolation)
    assert result.rule_name == "no-sunday-work"
    assert "scheduling" in result.reason.lower()


def test_non_match_passes_through(monkeypatch):
    """Rule that doesn't match returns None."""
    monkeypatch.setattr(
        check_mod, "_load_rules",
        lambda: [
            {
                "id": "no-pricing-below-15",
                "scope": "pricing",
                "condition": {"<": [{"var": "event.payload.margin_pct"}, 15]},
                "on_violate": {"explanation": "no commercial work below 15% margin"},
            },
        ],
    )
    chk = NonNegotiableChecker(adapter=None)
    ev = _make_event(raw_content="anything", scope="ops")

    assert chk.check(ev, scope="ops") is None


def test_empty_rules_no_op():
    """No rules loaded → check() returns None unconditionally."""
    # Don't monkeypatch _load_rules; the repo's foundation/05-non-negotiables.yaml
    # may or may not exist. Instead, force empty.
    chk = NonNegotiableChecker(adapter=None)
    chk.rules = []
    assert chk.check(_make_event()) is None
    assert chk.check(_make_event(), scope="anything") is None


def test_scope_filter_drops_other_scopes(monkeypatch):
    """A rule scoped to 'pricing' is not evaluated for an 'ops' event."""
    monkeypatch.setattr(
        check_mod, "_load_rules",
        lambda: [
            {
                "id": "pricing-rule",
                "scope": "pricing",
                "condition": True,  # would always match if evaluated
                "on_violate": {"explanation": "fires only for pricing"},
            },
        ],
    )
    chk = NonNegotiableChecker(adapter=None)
    assert chk.check(_make_event(), scope="ops") is None
    # But fires when the scope matches.
    hit = chk.check(_make_event(), scope="pricing")
    assert hit is not None
    assert hit.rule_name == "pricing-rule"


# ---------------------------------------------------------------------------
# LLM escape hatch
# ---------------------------------------------------------------------------

class _StubLLMClient:
    """Routes by system-prompt content like tests/test_session_runner.py does.

    Always returns a 'violation: true' verdict so we can assert the hatch
    is reached.
    """
    configured = True

    def __init__(self):
        self.calls = []

    def call(self, *, tier, system, user, json_mode=False, max_tokens=1024, temperature=0.2):
        self.calls.append({"tier": tier, "system": system[:60], "user_excerpt": user[:60]})

        class _R:
            text = json.dumps({"violation": True, "reason": "stub LLM flagged it"})

        return _R()

    @staticmethod
    def parse_json(text):
        try:
            return json.loads(text)
        except Exception:
            return None


def test_llm_escape_hatch_reachable(monkeypatch):
    """A check_type='llm' rule triggers the one-shot fast-tier LLM call."""
    monkeypatch.setattr(
        check_mod, "_load_rules",
        lambda: [
            {
                "id": "fuzzy-rule",
                "scope": "all",
                "check_type": "llm",
                "check_pattern": "Never email someone described as the owner's ex",
                "description": "no contact with ex",
            },
        ],
    )

    stub = _StubLLMClient()
    from solomon.reasoning import llm as llm_mod
    monkeypatch.setattr(llm_mod, "_client", stub)
    monkeypatch.setattr(llm_mod, "get_client", lambda: stub)
    # check_mod imports get_client at module load — patch its binding too.
    monkeypatch.setattr(check_mod, "get_client", lambda: stub)

    chk = NonNegotiableChecker(adapter=None)
    ev = _make_event(raw_content="draft an email to my ex")

    result = chk.check(ev)
    assert isinstance(result, NonNegotiableViolation)
    assert result.rule_name == "fuzzy-rule"
    assert "stub LLM flagged it" in result.reason
    assert len(stub.calls) == 1
    # Per session prompt: tier="fast" for the escape hatch.
    assert stub.calls[0]["tier"] == "fast"


def test_llm_escape_hatch_returns_none_when_unconfigured(monkeypatch):
    """If the LLM client isn't configured, an LLM rule degrades to no-match."""
    monkeypatch.setattr(
        check_mod, "_load_rules",
        lambda: [
            {
                "id": "fuzzy-rule",
                "scope": "all",
                "check_type": "llm",
                "check_pattern": "never do that",
            },
        ],
    )

    class _Unconfigured:
        configured = False

        def call(self, **kwargs):
            raise AssertionError("should not be called when unconfigured")

        @staticmethod
        def parse_json(text):
            return None

    from solomon.reasoning import llm as llm_mod
    stub = _Unconfigured()
    monkeypatch.setattr(llm_mod, "_client", stub)
    monkeypatch.setattr(llm_mod, "get_client", lambda: stub)
    monkeypatch.setattr(check_mod, "get_client", lambda: stub)

    chk = NonNegotiableChecker(adapter=None)
    assert chk.check(_make_event()) is None


def test_jsonlogic_first_short_circuits_llm(monkeypatch):
    """JSON-logic rules are evaluated before LLM rules; a hard-rule hit
    skips the LLM call entirely."""
    monkeypatch.setattr(
        check_mod, "_load_rules",
        lambda: [
            {
                "id": "hard-hit",
                "scope": "all",
                "condition": True,
                "on_violate": {"explanation": "always blocks"},
            },
            {
                "id": "fuzzy-rule",
                "scope": "all",
                "check_type": "llm",
                "check_pattern": "would never reach this",
            },
        ],
    )

    class _NeverCalled:
        configured = True
        calls = 0

        def call(self, **kwargs):
            type(self).calls += 1
            raise AssertionError("LLM rule must not be reached after a hard hit")

        @staticmethod
        def parse_json(text):
            return None

    from solomon.reasoning import llm as llm_mod
    stub = _NeverCalled()
    monkeypatch.setattr(llm_mod, "_client", stub)
    monkeypatch.setattr(llm_mod, "get_client", lambda: stub)
    monkeypatch.setattr(check_mod, "get_client", lambda: stub)

    chk = NonNegotiableChecker(adapter=None)
    result = chk.check(_make_event())
    assert result is not None
    assert result.rule_name == "hard-hit"
    assert _NeverCalled.calls == 0

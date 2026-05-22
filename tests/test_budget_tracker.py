"""Tests for the budget tracker."""
import os
from solomon.ingestion.budget_tracker import (
    can_spend,
    monthly_cap_tokens,
    record_spend,
    tokens_used_this_month,
)


def test_default_cap_is_one_million():
    # Default cap should be 1,000,000 tokens.
    os.environ.pop("SOLOMON_INGESTION_MONTHLY_CAP_TOKENS", None)
    assert monthly_cap_tokens("t1") == 1_000_000


def test_env_override_changes_cap():
    os.environ["SOLOMON_INGESTION_MONTHLY_CAP_TOKENS"] = "500000"
    try:
        assert monthly_cap_tokens("t1") == 500_000
    finally:
        os.environ.pop("SOLOMON_INGESTION_MONTHLY_CAP_TOKENS", None)


def test_can_spend_returns_true_when_under_cap():
    assert can_spend("t1", 1000) is True


def test_can_spend_returns_false_when_over_cap():
    os.environ["SOLOMON_INGESTION_MONTHLY_CAP_TOKENS"] = "100"
    try:
        assert can_spend("t1", 1000) is False
    finally:
        os.environ.pop("SOLOMON_INGESTION_MONTHLY_CAP_TOKENS", None)


def test_record_spend_does_not_crash():
    # Phase 1 just logs; should be a no-op that returns cleanly.
    record_spend("t1", 100)


def test_tokens_used_returns_zero_in_phase_1():
    # We don't yet track per-tenant tokens; the function returns 0 with
    # a TODO comment.
    assert tokens_used_this_month("t1") == 0

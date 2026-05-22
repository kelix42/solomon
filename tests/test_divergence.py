"""Tests for the divergence (surprise) score."""
from solomon.reasoning.divergence import divergence_score


def test_empty_strings_return_neutral():
    assert divergence_score("", "") == 0.5
    assert divergence_score("foo", "") == 0.5
    assert divergence_score("", "bar") == 0.5


def test_identical_strings_return_zero():
    assert divergence_score("send the email", "send the email") == 0.0


def test_no_overlap_returns_one():
    assert divergence_score("alpha beta", "gamma delta") == 1.0


def test_partial_overlap_returns_middle():
    score = divergence_score("send the email today", "send the email tomorrow")
    assert 0.0 < score < 1.0

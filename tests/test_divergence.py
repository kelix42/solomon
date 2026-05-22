"""Tests for the divergence (surprise) score.

The formula is hybrid: ``0.6 * jaccard_distance + 0.4 * (1 - length_ratio)``.
See ``solomon/reasoning/divergence.py`` for the rationale.
"""
from solomon.reasoning.divergence import divergence_score


def test_empty_strings_return_neutral():
    assert divergence_score("", "") == 0.5
    assert divergence_score("foo", "") == 0.5
    assert divergence_score("", "bar") == 0.5


def test_identical_strings_return_zero():
    assert divergence_score("send the email", "send the email") == 0.0


def test_no_overlap_same_length_returns_jaccard_component_only():
    # Both sides 11 chars long. Length ratio = 1.0, so length-term = 0.0.
    # Pure jaccard = 1.0. Final = 0.6 * 1.0 + 0.4 * 0.0 = 0.6.
    score = divergence_score("alpha beeta", "gamma delta")
    assert abs(score - 0.6) < 1e-9


def test_no_overlap_different_length_max_divergence():
    # Disjoint tokens AND wildly different length → highest divergence.
    score = divergence_score("a b c", "x" * 100)
    assert score > 0.9


def test_partial_overlap_returns_middle():
    score = divergence_score("send the email today", "send the email tomorrow")
    assert 0.0 < score < 1.0

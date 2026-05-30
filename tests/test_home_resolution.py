"""Home-folder resolution + split-brain guard.

Regression for the 2026-05-30 incident: Solomon ran with SOLOMON_HOME pointing
at ~/.hermes in some processes but the default ~/.hermes/solomon in others, so
onboarding answers landed in a folder the live gateway never read and Solomon
acted as if the profile were empty. These tests pin down:

1. home() resolution order (SOLOMON_HOME > $HERMES_HOME/solomon > default).
2. detect_stray_profiles() flags a profile.yaml in the home's parent dir.
"""

from __future__ import annotations

import os
from pathlib import Path

from solomon import logs, profile


# --- home() resolution order -------------------------------------------------


def test_home_prefers_solomon_home(monkeypatch, tmp_path):
    """Explicit SOLOMON_HOME wins over everything (tests/power users rely on this)."""
    monkeypatch.setenv("SOLOMON_HOME", str(tmp_path / "explicit"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    assert logs.home() == Path(str(tmp_path / "explicit"))


def test_home_anchors_on_hermes_home(monkeypatch, tmp_path):
    """With no SOLOMON_HOME, home is $HERMES_HOME/solomon — keeping Solomon's
    home aligned with whatever home Hermes itself is using."""
    monkeypatch.delenv("SOLOMON_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    assert logs.home() == Path(str(tmp_path / "hermes")) / "solomon"


def test_home_falls_back_to_default(monkeypatch):
    """Outside Hermes (no env at all), the documented default applies."""
    monkeypatch.delenv("SOLOMON_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert logs.home() == Path(os.path.expanduser("~/.hermes/solomon"))


# --- split-brain detection ---------------------------------------------------


def test_detect_stray_profile_in_parent(monkeypatch, tmp_path):
    """A profile.yaml in the home's PARENT dir is the split we hit — flag it."""
    canonical = tmp_path / "solomon"
    canonical.mkdir()
    monkeypatch.setenv("SOLOMON_HOME", str(canonical))
    (canonical / "profile.yaml").write_text("summary: {}\n")
    # Simulate the bug: a stray profile.yaml one level up.
    stray = tmp_path / "profile.yaml"
    stray.write_text("industry: {filled: true}\n")

    found = profile.detect_stray_profiles()
    assert found == [stray]


def test_no_stray_when_only_canonical_exists(monkeypatch, tmp_path):
    """The common, healthy case: nothing in the parent → no warning."""
    canonical = tmp_path / "solomon"
    canonical.mkdir()
    monkeypatch.setenv("SOLOMON_HOME", str(canonical))
    (canonical / "profile.yaml").write_text("summary: {}\n")

    assert profile.detect_stray_profiles() == []


def test_no_false_positive_on_solomon_home_override(monkeypatch, tmp_path):
    """A legitimate SOLOMON_HOME override must not be flagged just because a
    normal home exists elsewhere — only the parent dir is checked."""
    canonical = tmp_path / "custom" / "solomon"
    canonical.mkdir(parents=True)
    monkeypatch.setenv("SOLOMON_HOME", str(canonical))
    (canonical / "profile.yaml").write_text("summary: {}\n")
    # No profile.yaml in tmp_path/custom (the parent) → clean.
    assert profile.detect_stray_profiles() == []

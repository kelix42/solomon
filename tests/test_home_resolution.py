"""Home-folder resolution.

Regression for the 2026-05-30 split-brain incident: Solomon's home used to
depend on a SOLOMON_HOME override that some processes had set and others
didn't, so onboarding data landed in two different folders. Home is now
anchored solely on HERMES_HOME — the one variable Hermes sets identically for
the gateway, cron, and an interactive terminal — so every context agrees.
"""

from __future__ import annotations

import os
from pathlib import Path

from solomon import logs


def test_home_is_hermes_home_plus_solomon(monkeypatch, tmp_path):
    """Inside Hermes, home is always $HERMES_HOME/solomon."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert logs.home() == tmp_path / "solomon"


def test_home_falls_back_to_default_outside_hermes(monkeypatch):
    """Outside Hermes (no HERMES_HOME), the documented default applies."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert logs.home() == Path(os.path.expanduser("~/.hermes")) / "solomon"

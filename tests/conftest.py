"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def solomon_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point Solomon at a fresh tmp home for the duration of a test.

    Isolation is via HERMES_HOME — the same variable production uses — so
    tests exercise the real home() resolution path, not a test-only override.
    home() == $HERMES_HOME/solomon, which is what this fixture yields.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = tmp_path / "solomon"
    home.mkdir(parents=True, exist_ok=True)
    # Force the logger to reconfigure for this tmp path.
    from solomon import logs
    logs._configured = False
    yield home
    logs._configured = False

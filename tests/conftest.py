"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def solomon_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point Solomon at a fresh tmp directory for the duration of a test."""
    monkeypatch.setenv("SOLOMON_HOME", str(tmp_path))
    # Force the logger to reconfigure for this tmp path.
    from solomon import logs
    logs._configured = False
    yield tmp_path
    logs._configured = False

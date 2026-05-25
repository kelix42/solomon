"""Tests for the `solomon` CLI dispatcher."""

from __future__ import annotations

import io
import sys
from pathlib import Path

from solomon import cli


def test_cli_help(capsys):
    rc = cli.main(["help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "init" in out and "doctor" in out and "logs" in out


def test_cli_no_args_shows_help(capsys):
    rc = cli.main([])
    out = capsys.readouterr().out
    assert "Commands:" in out
    assert rc == 0


def test_cli_init_creates_home(solomon_home: Path, capsys):
    rc = cli.main(["init"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Home folder" in out
    assert (solomon_home / "profile.yaml").exists()


def test_cli_doctor_runs(solomon_home: Path, capsys):
    cli.main(["init"])
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "Solomon doctor" in out
    # Exit code 0 or 1 depending on environment; both are valid here.
    assert rc in (0, 1)


def test_cli_logs(solomon_home: Path, capsys):
    cli.main(["init"])
    from solomon import logs
    logs.log("test_event", scope="test")
    rc = cli.main(["logs", "--grep", "test_event"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "test_event" in out


def test_cli_unknown_command(capsys):
    rc = cli.main(["nonsense"])
    assert rc == 2

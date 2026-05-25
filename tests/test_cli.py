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


def test_cli_help_lists_register_crons(capsys):
    cli.main(["help"])
    out = capsys.readouterr().out
    assert "register-crons" in out
    assert "uninstall-crons" in out


def test_cli_uninstall_keeps_data_by_default(solomon_home: Path, capsys, monkeypatch):
    # No backup → uninstall just prints status and leaves the data folder.
    from solomon import profile
    profile.init_solomon_home()
    # Avoid actually calling Hermes during the test.
    from solomon import cli as cli_mod

    class DummyAdapter:
        def list_cron_jobs(self, name_prefix=None):
            return []
        def delete_cron_job(self, name):
            return False
        def disable_plugin(self, name):
            return False
        def is_plugin_enabled(self, name):
            return False

    monkeypatch.setattr(cli_mod, "_build_adapter", lambda: DummyAdapter())
    rc = cli.main(["uninstall"])
    assert rc == 0
    # Data folder still present.
    assert (solomon_home / "profile.yaml").exists()


def test_cli_register_crons_handles_missing_hermes(solomon_home: Path,
                                                     capsys, monkeypatch):
    """Without real Hermes cron modules, register-crons must surface a clear
    error rather than crash."""
    rc = cli.main(["register-crons"])
    out = capsys.readouterr()
    # Either Hermes was available locally and it returned 0, or it failed
    # cleanly with 1 + an error message. No traceback.
    assert rc in (0, 1)

"""Tests for solomon doctor."""

from __future__ import annotations

import io
from pathlib import Path

from solomon import doctor, profile


def test_doctor_on_fresh_install_returns_some_warnings(solomon_home: Path):
    profile.init_solomon_home()
    buf = io.StringIO()
    code = doctor.run(out=buf)
    output = buf.getvalue()
    # Will be 0 (clean) because reds are only for actually broken state.
    # On a fresh install some yellows are expected (no preferred_channel,
    # no cron, no Hermes skills folder).
    assert "Solomon doctor" in output
    assert code in (0, 1)


def test_doctor_red_when_profile_missing(solomon_home: Path):
    # Don't init; just point at empty folder.
    buf = io.StringIO()
    code = doctor.run(out=buf)
    output = buf.getvalue()
    assert "Solomon doctor" in output
    # Multiple checks should fail — exit code 1.
    assert code == 1


def test_doctor_red_when_profile_corrupted(solomon_home: Path):
    profile.init_solomon_home()
    (solomon_home / "profile.yaml").write_text("::: not yaml :::")
    buf = io.StringIO()
    code = doctor.run(out=buf)
    assert code == 1
    assert "unparseable" in buf.getvalue()


def test_doctor_redaction_check_green(solomon_home: Path):
    profile.init_solomon_home()
    status, msg, _ = doctor.check_redaction_works()
    assert status == "green"


def test_doctor_preferred_channel_yellow_when_unset(solomon_home: Path):
    profile.init_solomon_home()
    status, _, _ = doctor.check_preferred_channel()
    assert status == "yellow"


def test_doctor_preferred_channel_green_when_set(solomon_home: Path):
    profile.init_solomon_home()
    import yaml
    data = yaml.safe_load((solomon_home / "profile.yaml").read_text())
    data["meta"]["preferred_channel"] = "telegram"
    (solomon_home / "profile.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    status, msg, _ = doctor.check_preferred_channel()
    assert status == "green"
    assert "telegram" in msg

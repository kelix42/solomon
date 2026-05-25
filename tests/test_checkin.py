"""Tests for checkin.py — cron registration + manual fire."""

from __future__ import annotations

import sys
import types
from pathlib import Path

from solomon import checkin, profile


class FakeAdapter:
    def __init__(self):
        self._jobs: dict[str, dict] = {}

    def register_cron_job(self, *, name, schedule, prompt, skill=None,
                            skills=None, deliver="local",
                            enabled_toolsets=None, model=None, repeat=None):
        job = {"id": f"job_{name}", "name": name, "schedule": schedule,
                "prompt": prompt, "skill": skill, "deliver": deliver}
        self._jobs[name] = job
        return job

    def delete_cron_job(self, name):
        return self._jobs.pop(name, None) is not None

    def _find_cron_job_by_name(self, name):
        return self._jobs.get(name)


def test_register(solomon_home: Path):
    profile.init_solomon_home()
    a = FakeAdapter()
    job = checkin.register(a)
    assert job["name"] == "solomon-weekly-checkin"
    assert job["schedule"] == "0 15 * * 5"
    assert job["skill"] == "solomon-interview"
    assert job["deliver"] == "origin"
    assert "Weekly check-in" in job["prompt"]
    assert "[SILENT]" in job["prompt"]


def test_unregister(solomon_home: Path):
    profile.init_solomon_home()
    a = FakeAdapter()
    checkin.register(a)
    assert checkin.unregister(a) is True


def test_run_now_no_adapter(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    from solomon import tools
    monkeypatch.setattr(tools, "_adapter", None)
    out = checkin.run_now()
    assert out["ok"] is False


def test_run_now_fires(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    a = FakeAdapter()
    checkin.register(a)

    fake_scheduler = types.ModuleType("cron.scheduler")
    fired: list[dict] = []

    def fake_run_job(job):
        fired.append(job)
        return True, "out", "Hey — quick gap question.", None

    fake_scheduler.run_job = fake_run_job
    monkeypatch.setitem(sys.modules, "cron.scheduler", fake_scheduler)

    out = checkin.run_now(adapter=a)
    assert out["ok"] is True
    assert out["final_response"] == "Hey — quick gap question."
    assert fired[0]["name"] == "solomon-weekly-checkin"

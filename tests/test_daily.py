"""Tests for daily.py — Hermes-cron registration and manual fire."""

from __future__ import annotations

import sys
import types
from pathlib import Path

from solomon import daily, profile


class FakeAdapter:
    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self.registered: list[dict] = []
        self.deleted: list[str] = []

    def register_cron_job(self, *, name, schedule, prompt, skill=None,
                            skills=None, deliver="local",
                            enabled_toolsets=None, model=None, repeat=None):
        job = {"id": f"job_{name}", "name": name, "schedule": schedule,
                "prompt": prompt, "skill": skill, "deliver": deliver,
                "enabled_toolsets": enabled_toolsets}
        self._jobs[name] = job
        self.registered.append(job)
        return job

    def delete_cron_job(self, name):
        if name in self._jobs:
            del self._jobs[name]
            self.deleted.append(name)
            return True
        return False

    def _find_cron_job_by_name(self, name):
        return self._jobs.get(name)


def test_register_creates_one_job(solomon_home: Path):
    profile.init_solomon_home()
    a = FakeAdapter()
    job = daily.register(a)
    assert job["name"] == "solomon-daily-reflection"
    assert job["schedule"] == "0 2 * * *"
    assert job["skill"] == "solomon-ingest"
    assert job["deliver"] == "local"
    assert job["enabled_toolsets"] == ["solomon"]
    assert "read_conversations" in job["prompt"]
    assert "list_inbox" in job["prompt"]
    assert "[SILENT]" in job["prompt"]


def test_unregister(solomon_home: Path):
    profile.init_solomon_home()
    a = FakeAdapter()
    daily.register(a)
    assert daily.unregister(a) is True
    assert "solomon-daily-reflection" in a.deleted


def test_unregister_when_absent_returns_false(solomon_home: Path):
    profile.init_solomon_home()
    a = FakeAdapter()
    assert daily.unregister(a) is False


def test_run_now_without_adapter_returns_no_adapter(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    from solomon import tools
    monkeypatch.setattr(tools, "_adapter", None)
    out = daily.run_now()
    assert out["ok"] is False
    assert "no adapter" in out["reason"]


def test_run_now_fires_cron_job(solomon_home: Path, monkeypatch):
    """If Hermes's cron module is available, run_now hands off to it."""
    profile.init_solomon_home()
    a = FakeAdapter()
    daily.register(a)

    # Inject a fake cron.scheduler.
    fake_cron = types.ModuleType("cron")
    fake_cron.__path__ = []
    fake_jobs = types.ModuleType("cron.jobs")
    fake_scheduler = types.ModuleType("cron.scheduler")

    fired: list[dict] = []

    def fake_run_job(job: dict):
        fired.append(job)
        return True, "output doc", "final reply", None

    fake_scheduler.run_job = fake_run_job
    monkeypatch.setitem(sys.modules, "cron", fake_cron)
    monkeypatch.setitem(sys.modules, "cron.jobs", fake_jobs)
    monkeypatch.setitem(sys.modules, "cron.scheduler", fake_scheduler)

    out = daily.run_now(adapter=a)
    assert out["ok"] is True
    assert out["final_response"] == "final reply"
    assert fired[0]["name"] == "solomon-daily-reflection"


def test_run_now_auto_registers_if_missing(solomon_home: Path, monkeypatch):
    """If the cron isn't registered, run_now registers it first then fires."""
    profile.init_solomon_home()
    a = FakeAdapter()

    fake_cron = types.ModuleType("cron")
    fake_cron.__path__ = []
    fake_jobs = types.ModuleType("cron.jobs")
    fake_scheduler = types.ModuleType("cron.scheduler")
    fake_scheduler.run_job = lambda job: (True, "x", "y", None)
    monkeypatch.setitem(sys.modules, "cron", fake_cron)
    monkeypatch.setitem(sys.modules, "cron.jobs", fake_jobs)
    monkeypatch.setitem(sys.modules, "cron.scheduler", fake_scheduler)

    daily.run_now(adapter=a)
    # The job got registered as a side effect.
    assert "solomon-daily-reflection" in a._jobs

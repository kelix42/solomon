"""Tests for weekly.py — 15 staggered cron registrations + manual fire."""

from __future__ import annotations

import sys
import types
from pathlib import Path

from solomon import profile, weekly


class FakeAdapter:
    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self.registered: list[dict] = []
        self.deleted: list[str] = []

    def register_cron_job(self, *, name, schedule, prompt, skill=None,
                            skills=None, deliver="local",
                            enabled_toolsets=None, model=None, repeat=None):
        job = {"id": f"job_{name}", "name": name, "schedule": schedule,
                "prompt": prompt, "skill": skill, "deliver": deliver}
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


def test_register_creates_15_jobs(solomon_home: Path):
    profile.init_solomon_home()
    a = FakeAdapter()
    jobs = weekly.register(a)
    # 14 playbook compression jobs + 1 summary job
    assert len(jobs) == 15
    names = {j["name"] for j in jobs}
    for playbook in profile.PLAYBOOKS:
        assert f"solomon-compress-{playbook}" in names
    assert "solomon-regenerate-summary" in names


def test_schedules_are_staggered(solomon_home: Path):
    profile.init_solomon_home()
    a = FakeAdapter()
    weekly.register(a)
    schedules = [j["schedule"] for j in a.registered]
    # Distinct schedules (no duplicates) — staggered.
    assert len(set(schedules)) == len(schedules)


def test_schedules_start_at_03_00_sunday(solomon_home: Path):
    profile.init_solomon_home()
    a = FakeAdapter()
    weekly.register(a)
    # First playbook should be at minute=0, hour=3, day-of-week=0 (Sunday).
    first = a.registered[0]
    assert first["schedule"] == "0 3 * * 0"


def test_summary_schedule(solomon_home: Path):
    profile.init_solomon_home()
    a = FakeAdapter()
    weekly.register(a)
    summary_job = next(j for j in a.registered if j["name"] == "solomon-regenerate-summary")
    assert summary_job["schedule"] == "10 4 * * 0"
    assert "apply_profile_summary" in summary_job["prompt"]


def test_each_playbook_prompt_references_its_playbook(solomon_home: Path):
    profile.init_solomon_home()
    a = FakeAdapter()
    weekly.register(a)
    for playbook in profile.PLAYBOOKS:
        job = a._find_cron_job_by_name(f"solomon-compress-{playbook}")
        assert playbook in job["prompt"]
        assert "propose_compression" in job["prompt"]


def test_unregister_removes_all_15(solomon_home: Path):
    profile.init_solomon_home()
    a = FakeAdapter()
    weekly.register(a)
    count = weekly.unregister(a)
    assert count == 15
    assert a._jobs == {}


def test_run_now_one_playbook(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    a = FakeAdapter()
    weekly.register(a)

    fake_scheduler = types.ModuleType("cron.scheduler")
    fired: list[str] = []

    def fake_run_job(job):
        fired.append(job["name"])
        return True, "out", "done", None

    fake_scheduler.run_job = fake_run_job
    monkeypatch.setitem(sys.modules, "cron.scheduler", fake_scheduler)

    out = weekly.run_now(adapter=a, which="finance")
    assert out["ok"] is True
    assert fired == ["solomon-compress-finance"]


def test_run_now_all(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    a = FakeAdapter()
    weekly.register(a)

    fake_scheduler = types.ModuleType("cron.scheduler")
    fired: list[str] = []
    fake_scheduler.run_job = lambda job: (fired.append(job["name"]) or (True, "", "", None))
    monkeypatch.setitem(sys.modules, "cron.scheduler", fake_scheduler)

    out = weekly.run_now(adapter=a, which="all")
    assert len(fired) == 15


def test_run_now_summary_only(solomon_home: Path, monkeypatch):
    profile.init_solomon_home()
    a = FakeAdapter()
    weekly.register(a)

    fake_scheduler = types.ModuleType("cron.scheduler")
    fired: list[str] = []
    fake_scheduler.run_job = lambda job: (fired.append(job["name"]) or (True, "", "", None))
    monkeypatch.setitem(sys.modules, "cron.scheduler", fake_scheduler)

    weekly.run_now(adapter=a, which="summary")
    assert fired == ["solomon-regenerate-summary"]


def test_run_now_unknown_target(solomon_home: Path):
    profile.init_solomon_home()
    a = FakeAdapter()
    out = weekly.run_now(adapter=a, which="not_a_playbook")
    assert out["ok"] is False

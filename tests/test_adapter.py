"""Tests for the HermesAdapter — the only file with Hermes-specific names."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from solomon import adapter as adapter_mod
from solomon.adapter import HermesAdapter


# ---------------------------------------------------------------------------
# Constants — make sure we ship the exact Hermes-shaped values
# ---------------------------------------------------------------------------


def test_hook_constants_match_hermes_strings():
    assert adapter_mod.HOOK_PRE_LLM_CALL == "pre_llm_call"
    assert adapter_mod.HOOK_POST_LLM_CALL == "post_llm_call"
    assert adapter_mod.HOOK_ON_SESSION_START == "on_session_start"


def test_plugin_identity_constants():
    assert adapter_mod.PLUGIN_NAME == "solomon"
    assert adapter_mod.ENTRY_POINT_GROUP == "hermes_agent.plugins"
    assert adapter_mod.SOLOMON_TOOLSET == "solomon"


# ---------------------------------------------------------------------------
# FakeCtx — minimal Hermes ctx for unit tests
# ---------------------------------------------------------------------------


class FakeCtx:
    def __init__(self):
        self.tools: list[dict] = []
        self.commands: list[dict] = []
        self.hooks: list[tuple] = []

    def register_tool(self, *, name, toolset, schema, handler,
                       description="", emoji="", check_fn=None,
                       requires_env=None, is_async=False, override=False):
        self.tools.append({
            "name": name, "toolset": toolset, "schema": schema,
            "handler": handler, "description": description, "emoji": emoji,
        })

    def register_command(self, *, name, handler, description="", args_hint=""):
        self.commands.append({
            "name": name, "handler": handler,
            "description": description, "args_hint": args_hint,
        })

    def register_hook(self, hook_name, callback):
        self.hooks.append((hook_name, callback))

    def get_config(self, key, default=None):
        return default


# ---------------------------------------------------------------------------
# register_tool — uses Hermes's signature (toolset positional, schema=)
# ---------------------------------------------------------------------------


def test_register_tool_passes_toolset_and_schema():
    ctx = FakeCtx()
    a = HermesAdapter(ctx)
    a.register_tool(name="x", schema={"type": "object"},
                     handler=lambda d: None, description="desc")
    assert len(ctx.tools) == 1
    t = ctx.tools[0]
    assert t["name"] == "x"
    assert t["toolset"] == "solomon"
    assert t["schema"] == {"type": "object"}
    assert t["description"] == "desc"


def test_register_tool_allows_toolset_override():
    ctx = FakeCtx()
    a = HermesAdapter(ctx)
    a.register_tool(name="y", schema={}, handler=lambda d: None,
                     toolset="custom")
    assert ctx.tools[0]["toolset"] == "custom"


# ---------------------------------------------------------------------------
# register_command — handler takes (raw_args: str) -> str | None
# ---------------------------------------------------------------------------


def test_register_command_uses_hermes_signature():
    ctx = FakeCtx()
    a = HermesAdapter(ctx)
    a.register_command(name="onboard", description="onboard help",
                        handler=lambda raw: "hi")
    assert ctx.commands[0]["name"] == "onboard"
    # Handler accepts a string and returns a string.
    out = ctx.commands[0]["handler"]("session_0")
    assert out == "hi"


# ---------------------------------------------------------------------------
# register_hook — wraps ctx.register_hook
# ---------------------------------------------------------------------------


def test_register_hook_calls_ctx():
    ctx = FakeCtx()
    a = HermesAdapter(ctx)
    cb = lambda **kw: None
    a.register_hook(adapter_mod.HOOK_PRE_LLM_CALL, cb)
    assert ctx.hooks == [("pre_llm_call", cb)]


# ---------------------------------------------------------------------------
# Cron registration — uses a stubbed `cron.jobs` module
# ---------------------------------------------------------------------------


def _install_fake_cron_jobs(monkeypatch):
    """Inject a fake `cron.jobs` module that records calls."""
    pkg = types.ModuleType("cron")
    pkg.__path__ = []
    mod = types.ModuleType("cron.jobs")
    mod._jobs = []  # storage

    def parse_schedule(s):
        return {"raw": s}

    def create_job(*, prompt, schedule, name=None, **kwargs):
        job = {"id": f"job_{len(mod._jobs)}",
                "name": name, "prompt": prompt,
                "schedule": parse_schedule(schedule), **kwargs}
        mod._jobs.append(job)
        return job

    def list_jobs(include_disabled=False):
        return list(mod._jobs)

    def load_jobs():
        return list(mod._jobs)

    def save_jobs(jobs):
        mod._jobs[:] = jobs

    def get_job(job_id):
        for j in mod._jobs:
            if j.get("id") == job_id:
                return j
        return None

    def update_job(job_id, updates):
        for j in mod._jobs:
            if j.get("id") == job_id:
                j.update(updates)
                return j
        return None

    mod.parse_schedule = parse_schedule
    mod.create_job = create_job
    mod.list_jobs = list_jobs
    mod.load_jobs = load_jobs
    mod.save_jobs = save_jobs
    mod.get_job = get_job
    mod.update_job = update_job

    monkeypatch.setitem(sys.modules, "cron", pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", mod)
    return mod


def test_register_cron_job_creates_when_absent(monkeypatch):
    cron_jobs = _install_fake_cron_jobs(monkeypatch)
    a = HermesAdapter(FakeCtx())
    job = a.register_cron_job(name="solomon-daily-reflection",
                                schedule="0 2 * * *",
                                prompt="Do the daily reflection",
                                skill="solomon-ingest", deliver="local")
    assert job["name"] == "solomon-daily-reflection"
    assert len(cron_jobs._jobs) == 1


def test_register_cron_job_updates_when_present(monkeypatch):
    cron_jobs = _install_fake_cron_jobs(monkeypatch)
    a = HermesAdapter(FakeCtx())
    a.register_cron_job(name="solomon-test", schedule="0 2 * * *",
                         prompt="old prompt")
    a.register_cron_job(name="solomon-test", schedule="0 3 * * *",
                         prompt="new prompt")
    assert len(cron_jobs._jobs) == 1
    assert cron_jobs._jobs[0]["prompt"] == "new prompt"
    assert cron_jobs._jobs[0]["schedule"] == {"raw": "0 3 * * *"}


def test_list_cron_jobs_filters_by_prefix(monkeypatch):
    cron_jobs = _install_fake_cron_jobs(monkeypatch)
    a = HermesAdapter(FakeCtx())
    a.register_cron_job(name="solomon-daily-reflection",
                          schedule="0 2 * * *", prompt="x")
    a.register_cron_job(name="solomon-weekly-checkin",
                          schedule="0 15 * * 5", prompt="y")
    a.register_cron_job(name="someone-elses-job",
                          schedule="0 0 * * *", prompt="z")
    solomon_jobs = a.list_cron_jobs(name_prefix="solomon-")
    assert len(solomon_jobs) == 2
    assert all(j["name"].startswith("solomon-") for j in solomon_jobs)


def test_delete_cron_job_by_name(monkeypatch):
    cron_jobs = _install_fake_cron_jobs(monkeypatch)
    a = HermesAdapter(FakeCtx())
    a.register_cron_job(name="solomon-test", schedule="0 2 * * *", prompt="x")
    assert a.delete_cron_job("solomon-test") is True
    assert len(cron_jobs._jobs) == 0


def test_delete_cron_job_unknown_returns_false(monkeypatch):
    _install_fake_cron_jobs(monkeypatch)
    a = HermesAdapter(FakeCtx())
    assert a.delete_cron_job("does-not-exist") is False


# ---------------------------------------------------------------------------
# send_to_owner — uses a stubbed send_message_tool
# ---------------------------------------------------------------------------


def _install_fake_send_message_tool(monkeypatch, response):
    pkg = types.ModuleType("tools")
    pkg.__path__ = []
    mod = types.ModuleType("tools.send_message_tool")
    mod._calls = []

    def send_message_tool(args, **kw):
        mod._calls.append(args)
        return json.dumps(response)

    mod.send_message_tool = send_message_tool
    monkeypatch.setitem(sys.modules, "tools", pkg)
    monkeypatch.setitem(sys.modules, "tools.send_message_tool", mod)
    return mod


def test_send_to_owner_success(monkeypatch):
    fake = _install_fake_send_message_tool(monkeypatch, {"ok": True})
    a = HermesAdapter(FakeCtx())
    ok = a.send_to_owner("hello", target="telegram:12345")
    assert ok is True
    assert fake._calls[0]["message"] == "hello"
    assert fake._calls[0]["target"] == "telegram:12345"


def test_send_to_owner_error_returns_false(monkeypatch):
    _install_fake_send_message_tool(monkeypatch, {"error": "no such channel"})
    a = HermesAdapter(FakeCtx())
    assert a.send_to_owner("hi", target="telegram:bad") is False


def test_send_to_owner_no_target_falls_back_to_env(monkeypatch):
    fake = _install_fake_send_message_tool(monkeypatch, {"ok": True})
    monkeypatch.setenv("HERMES_TELEGRAM_HOME_CHAT_ID", "9999")
    a = HermesAdapter(FakeCtx())
    ok = a.send_to_owner("yo")
    assert ok is True
    assert fake._calls[0]["target"] == "telegram:9999"


def test_send_to_owner_no_target_and_no_env_returns_false(monkeypatch):
    _install_fake_send_message_tool(monkeypatch, {"ok": True})
    monkeypatch.delenv("HERMES_TELEGRAM_HOME_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_DISCORD_HOME_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SLACK_HOME_CHAT_ID", raising=False)
    a = HermesAdapter(FakeCtx())
    assert a.send_to_owner("yo") is False


# ---------------------------------------------------------------------------
# read_conversations — uses a stubbed hermes_state.SessionDB
# ---------------------------------------------------------------------------


def _install_fake_session_db(monkeypatch, sessions, messages_by_sid):
    mod = types.ModuleType("hermes_state")

    class SessionDB:
        def list_sessions_rich(self, limit=50):
            return sessions

        def get_messages_as_conversation(self, sid):
            return messages_by_sid.get(sid, [])

    mod.SessionDB = SessionDB
    monkeypatch.setitem(sys.modules, "hermes_state", mod)
    return mod


def test_read_conversations_returns_turns(monkeypatch):
    _install_fake_session_db(
        monkeypatch,
        sessions=[{"id": "s1", "started_at": 1700000000, "source": "telegram",
                    "title": "vendor call", "message_count": 4}],
        messages_by_sid={"s1": [{"role": "user", "content": "hi"},
                                  {"role": "assistant", "content": "hi back"}]},
    )
    a = HermesAdapter(FakeCtx())
    convos = a.read_conversations(limit=10)
    assert len(convos) == 1
    c = convos[0]
    assert c["session_id"] == "s1"
    assert c["title"] == "vendor call"
    assert len(c["turns"]) == 2


def test_read_conversations_filters_excluded_sessions(monkeypatch):
    _install_fake_session_db(
        monkeypatch,
        sessions=[{"id": "s1", "started_at": 1700000000},
                   {"id": "s2", "started_at": 1700000000}],
        messages_by_sid={"s1": [], "s2": []},
    )
    a = HermesAdapter(FakeCtx())
    convos = a.read_conversations(exclude_session_ids={"s1"})
    assert [c["session_id"] for c in convos] == ["s2"]


# ---------------------------------------------------------------------------
# is_plugin_enabled — reads ~/.hermes/config.yaml
# ---------------------------------------------------------------------------


def test_is_plugin_enabled_yes(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("plugins:\n  enabled:\n    - solomon\n", encoding="utf-8")
    monkeypatch.setattr(adapter_mod, "hermes_config_path", lambda: cfg)
    a = HermesAdapter(FakeCtx())
    assert a.is_plugin_enabled("solomon") is True


def test_is_plugin_enabled_missing_config(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(adapter_mod, "hermes_config_path",
                         lambda: tmp_path / "missing.yaml")
    a = HermesAdapter(FakeCtx())
    assert a.is_plugin_enabled("solomon") is False


def test_is_plugin_enabled_other_plugin(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("plugins:\n  enabled:\n    - other\n", encoding="utf-8")
    monkeypatch.setattr(adapter_mod, "hermes_config_path", lambda: cfg)
    a = HermesAdapter(FakeCtx())
    assert a.is_plugin_enabled("solomon") is False


# ---------------------------------------------------------------------------
# Plugin admin — wraps `hermes plugins enable/disable` via subprocess
# ---------------------------------------------------------------------------


def test_enable_plugin_invokes_hermes_cli(monkeypatch):
    captured: list[list] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        captured.append(argv)
        return Result()

    import subprocess as sp
    monkeypatch.setattr(sp, "run", fake_run)
    a = HermesAdapter(FakeCtx())
    assert a.enable_plugin("solomon") is True
    assert captured[0][-3:] == ["plugins", "enable", "solomon"]


def test_disable_plugin_failure_returns_false(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "not installed"

    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **kw: Result())
    a = HermesAdapter(FakeCtx())
    assert a.disable_plugin("solomon") is False


# ---------------------------------------------------------------------------
# Hermes-side paths
# ---------------------------------------------------------------------------


def test_hermes_paths():
    p = adapter_mod.hermes_config_path()
    assert p.name == "config.yaml"
    assert "hermes" in str(p)
    sd = adapter_mod.hermes_skills_dir_for("solomon")
    assert sd.name == "solomon"
    assert sd.parent.name == "skills"


# ---------------------------------------------------------------------------
# get_config — passthrough to ctx
# ---------------------------------------------------------------------------


def test_get_config_passthrough():
    class CtxWithConfig:
        def get_config(self, key, default=None):
            return {"foo": "bar"}.get(key, default)

    a = HermesAdapter(CtxWithConfig())
    assert a.get_config("foo") == "bar"
    assert a.get_config("missing", default="x") == "x"


def test_get_config_no_method_returns_default():
    a = HermesAdapter(FakeCtx())  # FakeCtx has get_config, so use a barer one
    class Bare:
        pass
    a = HermesAdapter(Bare())
    assert a.get_config("anything", default="d") == "d"

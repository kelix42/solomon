"""Tests for the skill-driven onboarding (onboarding_v2).

Covers:
  - session lifecycle (open_or_resume, abandon, complete)
  - the five LLM-callable tools (state, capture, complete, abandon, list)
  - the slash commands (/onboard, /endinterview, /onboarding)
  - the conductor injection hook (_maybe_inject_onboarding) — kill switch,
    happy path, no-active-session passthrough, exception fall-through.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

class TestSessionLifecycle:
    def test_open_or_resume_creates_row(self, solomon_db):
        from solomon.onboarding_v2.session import open_or_resume
        iv, resumed = open_or_resume("session_0")
        assert iv.domain == "industry"
        assert iv.ordinal == 0
        assert iv.skill_name == "solomon-onboarding-00-industry"
        assert iv.db_session_id.startswith("onboarding-00-industry-")
        # Brand-new row: resumed flag depends on whether today's stamp
        # already existed; for a fresh fixture it should be False.
        assert resumed is False

    def test_open_or_resume_resumes_existing_row(self, solomon_db):
        from solomon.onboarding_v2.session import open_or_resume
        iv1, _ = open_or_resume("session_0")
        iv2, _ = open_or_resume("session_0")
        assert iv1.db_session_id == iv2.db_session_id

    def test_open_or_resume_rejects_unknown_key(self, solomon_db):
        from solomon.onboarding_v2.session import open_or_resume
        with pytest.raises(ValueError):
            open_or_resume("session_99")

    def test_abandon_flips_status(self, solomon_db):
        from solomon.onboarding_v2.session import open_or_resume, abandon
        from solomon.storage.pool import get_conn, cursor, execute
        iv, _ = open_or_resume("session_0")
        abandon(iv.db_session_id)
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(cur, "SELECT status FROM sessions WHERE session_id=?", (iv.db_session_id,))
                row = cur.fetchone()
        assert row is not None
        assert (row[0] if not hasattr(row, "keys") else row["status"]) == "abandoned"

    def test_complete_flips_status(self, solomon_db):
        from solomon.onboarding_v2.session import open_or_resume, complete
        from solomon.storage.pool import get_conn, cursor, execute
        iv, _ = open_or_resume("session_0")
        complete(iv.db_session_id)
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(cur, "SELECT status FROM sessions WHERE session_id=?", (iv.db_session_id,))
                row = cur.fetchone()
        assert (row[0] if not hasattr(row, "keys") else row["status"]) == "complete"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class TestTools:
    def test_state_returns_unfilled_required_fields(self, solomon_db):
        from solomon.onboarding_v2 import session as sm
        from solomon.onboarding_v2.tools import _tool_state
        iv, _ = sm.open_or_resume("session_0")
        result = json.loads(_tool_state({"db_session_id": iv.db_session_id}))
        assert result["domain"] == "industry"
        assert result["status"] == "open"
        assert isinstance(result["required_fields"], list)
        assert len(result["required_fields"]) > 0
        # All should be unfilled in a fresh session.
        assert result["unfilled_count"] == len(result["required_fields"])
        assert result["complete_ready"] is False
        # Required fields each have id and prompt.
        for rf in result["required_fields"]:
            assert "id" in rf and "prompt" in rf and "filled" in rf

    def test_state_missing_session_id(self, solomon_db):
        from solomon.onboarding_v2.tools import _tool_state
        result = json.loads(_tool_state({}))
        assert "error" in result

    def test_capture_writes_row_with_field_tag(self, solomon_db):
        from solomon.onboarding_v2 import session as sm
        from solomon.onboarding_v2.tools import _tool_capture, _tool_state
        iv, _ = sm.open_or_resume("session_0")
        result = json.loads(_tool_capture({
            "db_session_id": iv.db_session_id,
            "statement": "Real estate law in Manitoba",
            "verbatim_phrase": "real estate law",
            "type": "preference",
            "field_id": "industry_label",
        }))
        assert result["status"] == "captured"
        assert "field:industry_label" in result["keywords"]

        # State now shows the field as filled.
        state = json.loads(_tool_state({"db_session_id": iv.db_session_id}))
        assert state["unfilled_count"] == len(state["required_fields"]) - 1
        industry_label = next(rf for rf in state["required_fields"] if rf["id"] == "industry_label")
        assert industry_label["filled"] is True

    def test_capture_rejects_missing_required_args(self, solomon_db):
        from solomon.onboarding_v2 import session as sm
        from solomon.onboarding_v2.tools import _tool_capture
        iv, _ = sm.open_or_resume("session_0")
        result = json.loads(_tool_capture({"db_session_id": iv.db_session_id}))
        assert "error" in result

    def test_complete_refuses_when_unfilled(self, solomon_db):
        from solomon.onboarding_v2 import session as sm
        from solomon.onboarding_v2.tools import _tool_complete
        iv, _ = sm.open_or_resume("session_0")
        result = json.loads(_tool_complete({"db_session_id": iv.db_session_id}))
        assert result["status"] == "not_ready"
        assert result["unfilled_count"] > 0

    def test_complete_with_force_renders_yaml(self, solomon_db, tmp_path, monkeypatch):
        # Point foundation rendering at tmp dir so we don't pollute the real one.
        from solomon.onboarding import session_runner as v1_runner
        foundation_dir = tmp_path / "foundation"
        monkeypatch.setattr(v1_runner, "FOUNDATION_DIR", foundation_dir, raising=True)

        from solomon.onboarding_v2 import session as sm
        from solomon.onboarding_v2.tools import _tool_capture, _tool_complete
        iv, _ = sm.open_or_resume("session_0")
        _tool_capture({
            "db_session_id": iv.db_session_id,
            "statement": "Real estate law",
            "verbatim_phrase": "real estate law",
            "type": "preference",
            "field_id": "industry_label",
        })
        result = json.loads(_tool_complete({
            "db_session_id": iv.db_session_id,
            "force": True,
        }))
        assert result["status"] == "complete"
        assert Path(result["yaml_path"]).exists()

    def test_abandon_marks_session_abandoned(self, solomon_db):
        from solomon.onboarding_v2 import session as sm
        from solomon.onboarding_v2.tools import _tool_abandon
        iv, _ = sm.open_or_resume("session_0")
        result = json.loads(_tool_abandon({"db_session_id": iv.db_session_id}))
        assert result["status"] == "abandoned"

    def test_list_returns_open_sessions(self, solomon_db):
        from solomon.onboarding_v2 import session as sm
        from solomon.onboarding_v2.tools import _tool_list
        sm.open_or_resume("session_0")
        result = json.loads(_tool_list({"status": "open"}))
        assert result["count"] >= 1
        assert any(s["domain"] == "industry" for s in result["sessions"])


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

class _StubAdapter:
    """Captures register_command / register_tool calls without needing a real Hermes ctx."""
    def __init__(self) -> None:
        self.commands: dict = {}
        self.tools: dict = {}

    def register_command(self, *, name, aliases=None, description, handler):  # noqa: D401
        self.commands[name] = handler

    def register_tool(self, *, name, description, parameters, handler, **kw):
        self.tools[name] = handler


class TestSlashCommands:
    def test_onboard_starts_session_and_returns_kickoff(self, solomon_db):
        from solomon.onboarding_v2.commands import OnboardingCommands
        from solomon.onboarding_v2.session import OnboardingSessionRegistry
        adapter = _StubAdapter()
        reg = OnboardingSessionRegistry()
        cmds = OnboardingCommands(adapter, reg)
        cmds.register_command()
        out = adapter.commands["onboard"](args="session_0", session_id="hermes-abc")
        assert "industry" in out.lower()
        assert reg.is_active("hermes-abc")

    def test_onboard_default_is_session_0(self, solomon_db):
        from solomon.onboarding_v2.commands import OnboardingCommands
        from solomon.onboarding_v2.session import OnboardingSessionRegistry
        adapter = _StubAdapter()
        reg = OnboardingSessionRegistry()
        OnboardingCommands(adapter, reg).register_command()
        out = adapter.commands["onboard"](args="", session_id="hermes-default")
        assert "industry" in out.lower()
        assert reg.is_active("hermes-default")

    def test_onboard_rejects_unknown_key(self, solomon_db):
        from solomon.onboarding_v2.commands import OnboardingCommands
        from solomon.onboarding_v2.session import OnboardingSessionRegistry
        adapter = _StubAdapter()
        reg = OnboardingSessionRegistry()
        OnboardingCommands(adapter, reg).register_command()
        out = adapter.commands["onboard"](args="session_99", session_id="x")
        assert "unknown" in out.lower()
        assert not reg.is_active("x")

    def test_onboard_refuses_when_already_active(self, solomon_db):
        from solomon.onboarding_v2.commands import OnboardingCommands
        from solomon.onboarding_v2.session import OnboardingSessionRegistry
        adapter = _StubAdapter()
        reg = OnboardingSessionRegistry()
        OnboardingCommands(adapter, reg).register_command()
        adapter.commands["onboard"](args="session_0", session_id="dup")
        out = adapter.commands["onboard"](args="session_1", session_id="dup")
        assert "already" in out.lower()

    def test_onboard_recovers_from_stale_registry_entry(self, solomon_db):
        """If the DB row was abandoned out-of-band (e.g. via the onboarding
        tool), /onboard must drop the stale in-memory registry entry and
        open a fresh session instead of refusing forever."""
        from solomon.onboarding_v2 import session as sm
        from solomon.onboarding_v2.commands import OnboardingCommands
        from solomon.onboarding_v2.session import OnboardingSessionRegistry
        adapter = _StubAdapter()
        reg = OnboardingSessionRegistry()
        OnboardingCommands(adapter, reg).register_command()

        # 1. Open a session via /onboard.
        adapter.commands["onboard"](args="session_0", session_id="stale")
        original = reg.get("stale")
        assert original is not None

        # 2. Abandon the DB row out-of-band (simulates the tool call path).
        sm.abandon(original.db_session_id)

        # 3. /onboard session_0 again should NOT refuse — it should open a new row.
        out = adapter.commands["onboard"](args="session_0", session_id="stale")
        assert "already" not in out.lower()
        assert "industry" in out.lower()
        new_iv = reg.get("stale")
        assert new_iv is not None
        assert new_iv.db_session_id != original.db_session_id

    def test_onboard_still_refuses_when_db_row_is_actually_open(self, solomon_db):
        """Belt-and-suspenders: when the DB really says open, refusal stands."""
        from solomon.onboarding_v2.commands import OnboardingCommands
        from solomon.onboarding_v2.session import OnboardingSessionRegistry
        adapter = _StubAdapter()
        reg = OnboardingSessionRegistry()
        OnboardingCommands(adapter, reg).register_command()
        adapter.commands["onboard"](args="session_0", session_id="real-open")
        out = adapter.commands["onboard"](args="session_0", session_id="real-open")
        assert "already" in out.lower()

    def test_endinterview_clears_active(self, solomon_db):
        from solomon.onboarding_v2.commands import OnboardingCommands
        from solomon.onboarding_v2.session import OnboardingSessionRegistry
        from solomon.storage.pool import get_conn, cursor, execute
        adapter = _StubAdapter()
        reg = OnboardingSessionRegistry()
        OnboardingCommands(adapter, reg).register_command()
        adapter.commands["onboard"](args="session_0", session_id="end-me")
        active = reg.get("end-me")
        assert active is not None
        sid = active.db_session_id
        adapter.commands["endinterview"](args="", session_id="end-me")
        assert not reg.is_active("end-me")
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(cur, "SELECT status FROM sessions WHERE session_id=?", (sid,))
                row = cur.fetchone()
        assert (row[0] if not hasattr(row, "keys") else row["status"]) == "abandoned"

    def test_endinterview_with_no_active_returns_friendly_message(self, solomon_db):
        from solomon.onboarding_v2.commands import OnboardingCommands
        from solomon.onboarding_v2.session import OnboardingSessionRegistry
        adapter = _StubAdapter()
        reg = OnboardingSessionRegistry()
        OnboardingCommands(adapter, reg).register_command()
        out = adapter.commands["endinterview"](args="", session_id="nothing-here")
        assert "no active" in out.lower()

    def test_onboarding_status_lists_all_sessions(self, solomon_db):
        from solomon.onboarding_v2.commands import OnboardingCommands
        from solomon.onboarding_v2.session import OnboardingSessionRegistry
        adapter = _StubAdapter()
        reg = OnboardingSessionRegistry()
        OnboardingCommands(adapter, reg).register_command()
        # No sessions yet → "not started" output.
        out = adapter.commands["onboarding"](args="", session_id="s")
        assert "session_0" in out.lower() or "no onboarding" in out.lower() or "not started" in out.lower()

        # After starting session_0:
        adapter.commands["onboard"](args="session_0", session_id="s")
        out2 = adapter.commands["onboarding"](args="", session_id="s")
        assert "industry" in out2.lower()


# ---------------------------------------------------------------------------
# Conductor injection hook
# ---------------------------------------------------------------------------

class TestConductorInjection:
    def _make_fake_conductor(self):
        """Build a minimal Conductor-shaped object that exposes the registry
        and the _maybe_inject_onboarding bound method without going through
        the full plugin bootstrap (which requires a real Hermes ctx).
        """
        from solomon.conductor import Conductor
        from solomon.onboarding_v2.session import OnboardingSessionRegistry
        # Build the instance manually via __new__ so we skip __init__ heavy work.
        c = Conductor.__new__(Conductor)
        c.onboarding_registry = OnboardingSessionRegistry()
        return c

    def test_no_active_session_passes_through(self, solomon_db):
        c = self._make_fake_conductor()
        messages = [{"role": "user", "content": "hi"}]
        injected = c._maybe_inject_onboarding("no-session", messages)
        assert injected is False
        assert len(messages) == 1  # untouched

    def test_active_session_injects_system_message(self, solomon_db):
        from solomon.onboarding_v2 import session as sm
        c = self._make_fake_conductor()
        iv, _ = sm.open_or_resume("session_0")
        c.onboarding_registry.register("active-1", iv)
        messages = [{"role": "user", "content": "real estate law"}]
        injected = c._maybe_inject_onboarding("active-1", messages)
        assert injected is True
        # Last message should be the injected system message.
        assert messages[-1]["role"] == "system"
        body = messages[-1]["content"]
        assert "Solomon onboarding" in body
        assert iv.db_session_id in body
        assert "industry" in body
        # The state JSON should be embedded.
        assert "required_fields" in body

    def test_injected_body_includes_skill_md_when_present(self, solomon_db):
        """If the skill file exists in HERMES_HOME/skills, it's read in."""
        from solomon.onboarding_v2 import session as sm
        from solomon.onboarding_v2.commands import _skill_path
        c = self._make_fake_conductor()
        iv, _ = sm.open_or_resume("session_0")
        c.onboarding_registry.register("with-skill", iv)
        messages = []
        c._maybe_inject_onboarding("with-skill", messages)
        body = messages[-1]["content"]
        # Even if the skill file is missing the body still ships — we just
        # check the marker is present.
        assert "=== BEGIN SKILL ===" in body
        assert "=== END SKILL ===" in body

    def test_messages_none_returns_false(self, solomon_db):
        c = self._make_fake_conductor()
        assert c._maybe_inject_onboarding("x", None) is False

    def test_kill_switch_via_pre_llm_call(self, solomon_db, monkeypatch):
        """SOLOMON_ONBOARDING_DISABLE=1 must skip the injection branch entirely."""
        from solomon.conductor import _onboarding_disabled
        monkeypatch.setenv("SOLOMON_ONBOARDING_DISABLE", "1")
        assert _onboarding_disabled() is True
        monkeypatch.setenv("SOLOMON_ONBOARDING_DISABLE", "")
        assert _onboarding_disabled() is False
        monkeypatch.setenv("SOLOMON_ONBOARDING_DISABLE", "true")
        assert _onboarding_disabled() is True

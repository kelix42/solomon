"""The one and only Hermes-shaped file.

Every Hermes API name, schema field, hook signature, cron field, and
config key that Solomon needs lives here. If Hermes ever changes one,
this is the only Solomon file to update.

The adapter exposes:
- Hook name constants     (HOOK_PRE_LLM_CALL, etc.)
- Toolset / plugin name   (SOLOMON_TOOLSET, PLUGIN_NAME)
- Registration wrappers   (register_tool, register_command, register_hook)
- Cron registration       (register_cron_job, list_cron_jobs, delete_cron_job)
- Owner messaging         (send_to_owner) — wraps tools.send_message_tool
- Session history         (read_conversations) — wraps hermes_state.SessionDB
- Config + paths          (is_plugin_enabled, hermes_config_path, hermes_skills_dir)
- Plugin admin            (enable_plugin, disable_plugin) — wraps `hermes plugins ...`
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from . import logs

# ---------------------------------------------------------------------------
# Constants — every Hermes-shaped name lives here
# ---------------------------------------------------------------------------

# Hook names. Source: hermes_cli/plugins.py:128 VALID_HOOKS set.
HOOK_PRE_LLM_CALL = "pre_llm_call"
HOOK_POST_LLM_CALL = "post_llm_call"
HOOK_ON_SESSION_START = "on_session_start"

# Plugin identity. Must match plugin.yaml and the entry-point in pyproject.toml.
PLUGIN_NAME = "solomon"
ENTRY_POINT_GROUP = "hermes_agent.plugins"
SOLOMON_TOOLSET = "solomon"

# Filesystem layout (Hermes side).
HERMES_HOME = Path.home() / ".hermes"


# ---------------------------------------------------------------------------
# Path helpers — Hermes-side filesystem layout
# ---------------------------------------------------------------------------


def hermes_config_path() -> Path:
    return HERMES_HOME / "config.yaml"


def hermes_skills_dir() -> Path:
    return HERMES_HOME / "skills"


def hermes_skills_dir_for(plugin_name: str = PLUGIN_NAME) -> Path:
    return hermes_skills_dir() / plugin_name


# ---------------------------------------------------------------------------
# The adapter itself
# ---------------------------------------------------------------------------


class HermesAdapter:
    """Wraps the ctx Hermes passes to register(ctx)."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    # -- Tools ------------------------------------------------------------

    def register_tool(self, *, name: str, schema: dict, handler: Callable,
                      description: str = "", emoji: str = "",
                      toolset: str = SOLOMON_TOOLSET) -> None:
        """Register a tool. Real Hermes signature: register_tool(name, toolset,
        schema, handler, ...). We always use the same toolset for Solomon
        unless the caller overrides (which they shouldn't).
        """
        self.ctx.register_tool(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            description=description,
            emoji=emoji,
        )

    # -- Slash commands ---------------------------------------------------

    def register_command(self, *, name: str, description: str,
                          handler: Callable[[str], Optional[str]],
                          args_hint: str = "") -> None:
        """Register a slash command. Hermes signature:
            handler(raw_args: str) -> str | None
        The returned string is what the owner sees as the command's reply.
        """
        self.ctx.register_command(
            name=name,
            handler=handler,
            description=description,
            args_hint=args_hint,
        )

    # -- Hooks ------------------------------------------------------------

    def register_hook(self, hook_name: str, callback: Callable) -> None:
        """Register a Hermes lifecycle hook. Callbacks are called with
        keyword args (kwargs-only). For pre_llm_call, the return value
        — a dict {"context": "..."} or a plain string — gets injected
        into the user message (not the system prompt) so the prompt-cache
        prefix stays stable. See hermes_cli/plugins.py:1495-1529.
        """
        self.ctx.register_hook(hook_name, callback)

    # -- Cron jobs --------------------------------------------------------

    def register_cron_job(self, *, name: str, schedule: str, prompt: str,
                          skill: Optional[str] = None,
                          skills: Optional[list[str]] = None,
                          deliver: str = "local",
                          enabled_toolsets: Optional[list[str]] = None,
                          model: Optional[str] = None,
                          repeat: Optional[int] = None) -> dict:
        """Idempotent: if a job with the same name exists, update it.

        Returns the job dict that landed in Hermes's cron storage.
        """
        # Lazy import — Hermes only available inside its own venv.
        from cron import jobs as cron_jobs

        existing = self._find_cron_job_by_name(name)
        if existing:
            updates = {
                "schedule": cron_jobs.parse_schedule(schedule),
                "prompt": prompt,
                "deliver": deliver,
            }
            if skill is not None:
                updates["skill"] = skill
            if skills is not None:
                updates["skills"] = skills
            if enabled_toolsets is not None:
                updates["enabled_toolsets"] = enabled_toolsets
            if model is not None:
                updates["model"] = model
            cron_jobs.update_job(existing["id"], updates)
            logs.log("cron_job_updated", context={"name": name, "id": existing["id"]})
            return cron_jobs.get_job(existing["id"]) or existing
        job = cron_jobs.create_job(
            prompt=prompt,
            schedule=schedule,
            name=name,
            skill=skill,
            skills=skills,
            deliver=deliver,
            enabled_toolsets=enabled_toolsets,
            model=model,
            repeat=repeat,
        )
        logs.log("cron_job_created", context={"name": name, "id": job.get("id")})
        return job

    def list_cron_jobs(self, name_prefix: Optional[str] = None) -> list[dict]:
        from cron import jobs as cron_jobs
        all_jobs = cron_jobs.list_jobs(include_disabled=True)
        if name_prefix is None:
            return all_jobs
        return [j for j in all_jobs if str(j.get("name", "")).startswith(name_prefix)]

    def delete_cron_job(self, job_id_or_name: str) -> bool:
        from cron import jobs as cron_jobs
        target = cron_jobs.get_job(job_id_or_name)
        if target is None:
            target = self._find_cron_job_by_name(job_id_or_name)
        if target is None:
            return False
        # Hermes deletes by overwriting the jobs file without this entry.
        all_jobs = cron_jobs.load_jobs()
        new_jobs = [j for j in all_jobs if j.get("id") != target["id"]]
        if len(new_jobs) == len(all_jobs):
            return False
        cron_jobs.save_jobs(new_jobs)
        logs.log("cron_job_deleted", context={"id": target["id"], "name": target.get("name")})
        return True

    def _find_cron_job_by_name(self, name: str) -> Optional[dict]:
        from cron import jobs as cron_jobs
        for j in cron_jobs.load_jobs():
            if j.get("name") == name:
                return j
        return None

    # -- Owner messaging --------------------------------------------------

    def send_to_owner(self, text: str, target: Optional[str] = None) -> bool:
        """Push a message to the owner. `target` is a "platform:channel-id"
        string per send_message_tool's spec; when None, the gateway's
        configured home channel is used.

        Returns True on apparent success (no error returned from the tool),
        False otherwise. The caller is responsible for queuing into
        pending_messages.jsonl on False.
        """
        try:
            from tools.send_message_tool import send_message_tool
        except ImportError as e:
            logs.log_error("error", e, where="adapter.send_to_owner (import)")
            return False
        if target is None:
            # Fall back to the platform's home channel via the gateway config.
            target = self._default_owner_target()
            if not target:
                logs.log("hermes_api_missing", level="WARN",
                         context={"method": "send_to_owner",
                                  "reason": "no target and no home channel configured"})
                return False
        try:
            import json
            result_json = send_message_tool({"action": "send",
                                              "target": target,
                                              "message": text})
            result = json.loads(result_json) if isinstance(result_json, str) else result_json
            if isinstance(result, dict) and "error" in result:
                logs.log("send_to_owner_failed", level="WARN",
                         context={"target": target, "error": result.get("error")})
                return False
            return True
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where="adapter.send_to_owner")
            return False

    def _default_owner_target(self) -> Optional[str]:
        """Pick a sensible default target for owner-bound messages.

        Looks at gateway config for a home channel env var. Returns a
        "platform:chat_id" string or None.
        """
        try:
            from gateway.platform_registry import platform_registry
            for entry in platform_registry.plugin_entries():
                env_var = getattr(entry, "cron_deliver_env_var", None)
                if env_var:
                    chat_id = os.getenv(env_var)
                    if chat_id:
                        return f"{entry.name}:{chat_id}"
        except Exception:  # noqa: BLE001
            pass
        # Built-in platforms with conventional env vars.
        for platform, env in [
            ("telegram", "HERMES_TELEGRAM_HOME_CHAT_ID"),
            ("discord", "HERMES_DISCORD_HOME_CHAT_ID"),
            ("slack", "HERMES_SLACK_HOME_CHAT_ID"),
        ]:
            chat_id = os.getenv(env)
            if chat_id:
                return f"{platform}:{chat_id}"
        return None

    # -- Session / conversation history ----------------------------------

    def read_conversations(self, since: Optional[datetime] = None,
                           limit: int = 50,
                           exclude_session_ids: Optional[set[str]] = None) -> list[dict]:
        """Return recent Hermes conversations.

        Each dict: {session_id, started_at, ended_at, source, title,
        message_count, turns (list of {role, content, ts})}.

        `since` filters by started_at (default: last 24h).
        `exclude_session_ids` lets the caller filter out Solomon's private
        sessions before any LLM sees the content.
        """
        try:
            from hermes_state import SessionDB
        except ImportError as e:
            logs.log_error("error", e, where="adapter.read_conversations (import)")
            return []
        db = SessionDB()
        try:
            session_rows = db.list_sessions_rich(limit=limit)
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where="adapter.read_conversations.list")
            return []
        out: list[dict] = []
        excluded = exclude_session_ids or set()
        for row in session_rows:
            sid = row.get("id") or row.get("session_id")
            if not sid or sid in excluded:
                continue
            started_at = row.get("started_at")
            if since is not None and started_at is not None:
                # started_at is a unix timestamp (float per Hermes schema).
                try:
                    if float(started_at) < since.timestamp():
                        continue
                except (TypeError, ValueError):
                    pass
            try:
                turns = db.get_messages_as_conversation(sid)
            except Exception as e:  # noqa: BLE001
                logs.log_error("error", e, where="adapter.read_conversations.messages",
                                context={"session_id": sid})
                turns = []
            out.append({
                "session_id": sid,
                "started_at": started_at,
                "ended_at": row.get("ended_at"),
                "source": row.get("source"),
                "title": row.get("title"),
                "message_count": row.get("message_count"),
                "turns": turns,
            })
        return out

    # -- Plugin admin -----------------------------------------------------

    def is_plugin_enabled(self, name: str = PLUGIN_NAME) -> bool:
        try:
            import yaml
            with hermes_config_path().open("r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except (FileNotFoundError, yaml.YAMLError):
            return False
        enabled = (cfg.get("plugins") or {}).get("enabled") or []
        return name in (enabled if isinstance(enabled, list) else [])

    def enable_plugin(self, name: str = PLUGIN_NAME) -> bool:
        return self._run_plugins_cmd("enable", name)

    def disable_plugin(self, name: str = PLUGIN_NAME) -> bool:
        return self._run_plugins_cmd("disable", name)

    def _run_plugins_cmd(self, action: str, name: str) -> bool:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "hermes_cli", "plugins", action, name],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                logs.log("hermes_plugins_cmd_failed", level="WARN",
                         context={"action": action, "name": name,
                                   "stderr": result.stderr[:500]})
                return False
            return True
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where=f"adapter._run_plugins_cmd({action})")
            return False

    # -- Config ----------------------------------------------------------

    def get_config(self, key: str, default: Any = None) -> Any:
        getter = getattr(self.ctx, "get_config", None)
        if getter is None:
            return default
        try:
            return getter(key, default)
        except TypeError:
            return getter(key) or default

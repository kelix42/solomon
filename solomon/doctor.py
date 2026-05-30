"""`solomon doctor` — health check.

Runs a battery of cheap checks on a live install and prints color-coded
status for each. Returns exit code 0 if everything is green, 1 if any
check is red.

Yellow checks are warnings (functional but missing nice-to-have config).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

import yaml

from . import adapter as adapter_mod, logs, profile
from .adapter import HermesAdapter


# ANSI colors. Disabled if not a TTY (so log capture stays clean).
def _colorize(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


GREEN = lambda s: _colorize(s, "32")
YELLOW = lambda s: _colorize(s, "33")
RED = lambda s: _colorize(s, "31")
BOLD = lambda s: _colorize(s, "1")


# Each check returns (status, message, remedy_or_None) where status is
# 'green' / 'yellow' / 'red'.


def check_home_exists() -> tuple[str, str, Optional[str]]:
    h = profile.home()
    if os.getenv("SOLOMON_HOME"):
        how = "SOLOMON_HOME"
    elif os.getenv("HERMES_HOME"):
        how = "$HERMES_HOME/solomon"
    else:
        how = "default ~/.hermes/solomon"
    if not h.exists():
        return "red", f"Solomon home folder missing: {h}", "Run `solomon init`."
    return "green", f"Solomon home: {h}  (resolved via {how})", None


def check_no_split_home() -> tuple[str, str, Optional[str]]:
    """Catch the split-brain home bug: a second profile.yaml living outside
    the canonical home, holding data the live gateway never reads."""
    stray = profile.detect_stray_profiles()
    if stray:
        locations = ", ".join(str(p) for p in stray)
        canonical = profile.home() / "profile.yaml"
        return "red", f"Split-brain home: profile.yaml also exists at {locations}", \
               (f"Solomon reads {canonical}. Compare it with the stray file(s); "
                "copy the fuller one into the canonical home, then move the stray "
                "aside so this can't silently re-split.")
    return "green", "No stray profile.yaml outside the canonical home", None


def check_profile_parses() -> tuple[str, str, Optional[str]]:
    path = profile.home() / "profile.yaml"
    if not path.exists():
        return "red", "profile.yaml missing", "Run `solomon init`."
    try:
        yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return "red", f"profile.yaml unparseable: {e}", \
               "Restore with `git restore profile.yaml` inside ~/.hermes/solomon/."
    return "green", "profile.yaml parses cleanly", None


def check_all_playbooks_exist() -> tuple[str, str, Optional[str]]:
    missing = [n for n in profile.PLAYBOOKS
               if not (profile.home() / f"{n}.md").exists()]
    if missing:
        return "red", f"Missing playbooks: {missing}", "Run `solomon init`."
    return "green", "All 14 playbooks present", None


def check_queues_exist() -> tuple[str, str, Optional[str]]:
    rq = (profile.home() / "review_queue.jsonl").exists()
    pa = (profile.home() / "pending_actions.jsonl").exists()
    if not rq or not pa:
        return "red", "Queue files missing", "Run `solomon init`."
    return "green", "Queue files present", None


def check_git_repo() -> tuple[str, str, Optional[str]]:
    if not (profile.home() / ".git").exists():
        return "yellow", "Solomon home is not a git repo", "Run `solomon init` to initialize."
    # Check for uncommitted changes (other than the intentionally-untracked transient files).
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(profile.home()), capture_output=True, text=True, check=False
        )
        dirty = result.stdout.strip()
        if dirty:
            return "yellow", "Solomon home git repo has uncommitted changes", \
                   "Some change failed to commit. Try `git status` in ~/.hermes/solomon/."
    except FileNotFoundError:
        return "yellow", "git binary not on PATH", "Install git."
    return "green", "Git repo clean", None


def check_skill_files() -> tuple[str, str, Optional[str]]:
    skill_dir = adapter_mod.hermes_skills_dir_for("solomon")
    expected = ("solomon-default.md", "solomon-interview.md",
                "solomon-ingest.md", "solomon-compress.md")
    if not skill_dir.exists():
        return "red", f"Skills not installed at {skill_dir}", \
               "Re-run the install script."
    missing = [f for f in expected if not (skill_dir / f).exists()]
    if missing:
        return "red", f"Missing skill files: {missing}", "Re-run the install script."
    return "green", f"All 4 skill files in {skill_dir}", None


def check_hermes_config() -> tuple[str, str, Optional[str]]:
    cfg = adapter_mod.hermes_config_path()
    if not cfg.exists():
        return "yellow", "~/.hermes/config.yaml missing — Hermes may not be installed", \
               "Install Hermes first, then re-run the Solomon installer."
    a = HermesAdapter(ctx=None)  # ctx not needed for is_plugin_enabled
    try:
        enabled = a.is_plugin_enabled("solomon")
    except Exception as e:  # noqa: BLE001
        return "yellow", f"could not check plugins.enabled: {e}", \
               "Re-run the install script."
    if not enabled:
        return "yellow", "Solomon not in plugins.enabled of Hermes config", \
               "Run `hermes plugins enable solomon` or re-run the install script."
    return "green", "Solomon registered in Hermes config", None


def check_cron_installed() -> tuple[str, str, Optional[str]]:
    """Verify Solomon's 17 Hermes cron jobs are registered.

    Solomon registers cron jobs through Hermes's cron API, not the system
    crontab — so we ask Hermes, not the OS.
    """
    a = HermesAdapter(ctx=None)
    try:
        jobs = a.list_cron_jobs(name_prefix="solomon-")
    except Exception as e:  # noqa: BLE001
        return "yellow", f"could not query Hermes cron: {e}", \
               "Run `solomon register-crons` or re-run the install script."
    names = {j.get("name") for j in jobs}
    # Expected: 1 daily + 14 weekly compress + 1 summary + 1 checkin = 17.
    expected_names = ({"solomon-daily-reflection",
                        "solomon-regenerate-summary",
                        "solomon-weekly-checkin"} |
                       {f"solomon-compress-{p}" for p in profile.PLAYBOOKS})
    missing = expected_names - names
    if missing:
        return "yellow", f"Missing Hermes cron jobs: {sorted(missing)}", \
               "Run `solomon register-crons` to (re-)register them."
    return "green", f"{len(names)} Solomon cron jobs registered with Hermes", None


def check_logs_writable() -> tuple[str, str, Optional[str]]:
    try:
        logs.log("health_check")
    except Exception as e:  # noqa: BLE001
        return "red", f"Cannot write logs: {e}", \
               f"Check permissions on {logs.log_path()}."
    if not logs.log_path().exists():
        return "red", f"Log file did not appear at {logs.log_path()}", \
               f"Check permissions on {logs.log_path().parent}."
    return "green", "Logs writable", None


def check_redaction_works() -> tuple[str, str, Optional[str]]:
    out = profile.redact("SSN 123-45-6789")
    if out == "SSN [SSN]":
        return "green", "PII redaction working", None
    return "red", f"Redaction not working: got {out!r}", \
           "File a bug — this should not happen on a fresh install."


def check_preferred_channel() -> tuple[str, str, Optional[str]]:
    try:
        data = yaml.safe_load((profile.home() / "profile.yaml").read_text())
    except Exception:  # noqa: BLE001
        return "yellow", "Cannot read profile.yaml to check preferred_channel", None
    ch = (data.get("meta") or {}).get("preferred_channel") or ""
    if not ch:
        return "yellow", "preferred_channel not set in profile.yaml.meta", \
               "Finish session 6 of /onboard to set this."
    return "green", f"preferred_channel: {ch}", None


CHECKS: list[tuple[str, Callable[[], tuple[str, str, Optional[str]]]]] = [
    ("home folder", check_home_exists),
    ("split-home", check_no_split_home),
    ("profile.yaml", check_profile_parses),
    ("playbooks", check_all_playbooks_exist),
    ("queues", check_queues_exist),
    ("git repo", check_git_repo),
    ("skill files", check_skill_files),
    ("hermes config", check_hermes_config),
    ("cron jobs", check_cron_installed),
    ("logs", check_logs_writable),
    ("PII redaction", check_redaction_works),
    ("preferred channel", check_preferred_channel),
]


def run(out=None) -> int:
    """Run all checks. Return 0 if all green/yellow, 1 if any red."""
    out = out or sys.stdout
    print(BOLD("Solomon doctor"), file=out)
    print("", file=out)
    any_red = False
    for label, fn in CHECKS:
        try:
            status, msg, remedy = fn()
        except Exception as e:  # noqa: BLE001
            status, msg, remedy = "red", f"check failed: {e}", None
        icon = {"green": GREEN("✓"), "yellow": YELLOW("!"), "red": RED("✗")}[status]
        print(f"  {icon} {label:<20} {msg}", file=out)
        if remedy and status != "green":
            print(f"      → {remedy}", file=out)
        if status == "red":
            any_red = True
    print("", file=out)
    if any_red:
        print(RED("Some checks failed. Address the red items above."), file=out)
        return 1
    print(GREEN("All clear."), file=out)
    return 0


def main() -> int:
    return run()

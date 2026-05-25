"""`solomon` CLI dispatcher.

Subcommands:
  solomon init       — scaffold ~/.hermes/solomon/ and run first-time setup
  solomon doctor     — health check
  solomon logs       — view structured logs
  solomon uninstall  — restore Hermes config (data left in place)

Cron subcommands (called by the cron entries directly, but also runnable
manually for testing):
  solomon daily      — run nightly reflection + ingestion + nudge
  solomon weekly     — run weekly compression
  solomon checkin    — run weekly check-in
  solomon ingest     — run one ingest pass on the inbox

These match the slash commands' bodies. Slash commands (typed inside
Hermes) call the same code paths.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("--help", "-h", "help"):
        _print_help()
        return 0
    sub, rest = argv[0], argv[1:]

    if sub == "init":
        return _cmd_init()
    if sub == "doctor":
        from . import doctor
        return doctor.run()
    if sub == "logs":
        return _cmd_logs(rest)
    if sub == "uninstall":
        return _cmd_uninstall()
    if sub == "daily":
        from . import daily
        result = daily.run()
        print(result)
        return 0
    if sub == "weekly":
        from . import weekly
        result = weekly.run()
        print(result)
        return 0
    if sub == "checkin":
        from . import checkin
        result = checkin.run()
        print(result)
        return 0
    if sub == "ingest":
        from . import ingest
        result = ingest.process_all()
        print(result)
        return 0

    print(f"Unknown command: {sub}", file=sys.stderr)
    _print_help(out=sys.stderr)
    return 2


def _print_help(out=None) -> None:
    out = out or sys.stdout
    print(
        "Usage: solomon <command> [args]\n\n"
        "Commands:\n"
        "  init       Scaffold ~/.hermes/solomon/ and finish setup.\n"
        "  doctor     Check that everything is wired right.\n"
        "  logs       View structured logs. Flags: --errors, --today, --since DATE,\n"
        "             --grep PATTERN, --event NAME, --follow.\n"
        "  daily      Run the nightly reflection + ingestion + nudge cycle now.\n"
        "  weekly     Run the weekly compression now.\n"
        "  checkin    Send the weekly check-in now.\n"
        "  ingest     Process anything in ~/.hermes/solomon/inbox/ now.\n"
        "  uninstall  Restore the pre-Solomon Hermes config (data is left in place).\n",
        file=out,
    )


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def _cmd_init() -> int:
    from . import logs, profile
    print("Setting up Solomon...")
    home = profile.init_solomon_home()
    print(f"  ✓ Home folder: {home}")
    print(f"  ✓ All 14 playbooks created (empty templates)")
    print(f"  ✓ profile.yaml created (empty foundation)")
    print(f"  ✓ Git repo initialized")
    logs.log("init_complete")
    print()
    print("Next:")
    print("  Open Hermes and type /onboard to start the foundation interview.")
    print("  Or type `solomon doctor` to confirm everything is wired right.")
    return 0


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


def _cmd_logs(args: list[str]) -> int:
    p = argparse.ArgumentParser(prog="solomon logs", add_help=True)
    p.add_argument("--errors", action="store_true")
    p.add_argument("--today", action="store_true")
    p.add_argument("--since", default=None)
    p.add_argument("--grep", default=None)
    p.add_argument("--event", default=None)
    p.add_argument("--follow", "-f", action="store_true")
    opts = p.parse_args(args)
    from . import logs
    logs.view(errors_only=opts.errors, today_only=opts.today,
              since=opts.since, grep=opts.grep, event=opts.event,
              follow=opts.follow)
    return 0


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def _cmd_uninstall() -> int:
    from pathlib import Path
    import shutil

    cfg = Path.home() / ".hermes" / "config.yaml"
    backup = Path.home() / ".hermes" / "config.yaml.pre-solomon"
    if backup.exists():
        shutil.copy2(backup, cfg)
        print(f"  ✓ Restored {cfg} from {backup}")
    else:
        print("  (no backup found; Hermes config unchanged)")
    print()
    print("Solomon data at ~/.hermes/solomon/ is left in place.")
    print("Delete that folder if you want a clean slate.")
    print("Solomon skill files at ~/.hermes/skills/solomon/ are also left in place.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""`solomon` CLI dispatcher.

Subcommands:
  solomon init             Scaffold ~/.hermes/solomon/ and finish setup.
  solomon doctor           Health check.
  solomon logs             View structured logs.
  solomon register-crons   Register Solomon's 17 cron jobs with Hermes.
  solomon uninstall-crons  Remove every Solomon cron from Hermes.
  solomon daily            Fire the daily reflection cron now.
  solomon weekly           Fire all weekly compression crons now.
  solomon checkin          Fire the weekly check-in cron now.
  solomon ingest           Process ~/.hermes/solomon/inbox/ now.
  solomon uninstall        Restore pre-Solomon Hermes config.
    --purge                Also delete ~/.hermes/solomon/ after a prompt.
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
    if sub == "register-crons":
        return _cmd_register_crons()
    if sub == "uninstall-crons":
        return _cmd_uninstall_crons()
    if sub == "daily":
        from . import daily
        result = daily.run_now(adapter=_build_adapter())
        print(result)
        return 0 if result.get("ok") else 1
    if sub == "weekly":
        from . import weekly
        which = rest[0] if rest else "all"
        result = weekly.run_now(which=which, adapter=_build_adapter())
        print(result)
        return 0 if result.get("ok") else 1
    if sub == "checkin":
        from . import checkin
        result = checkin.run_now(adapter=_build_adapter())
        print(result)
        return 0 if result.get("ok") else 1
    if sub == "ingest":
        # Same flow as /ingest in Hermes — fires the daily reflection cron.
        from . import daily
        result = daily.run_now(adapter=_build_adapter())
        print(result)
        return 0 if result.get("ok") else 1
    if sub == "uninstall":
        purge = "--purge" in rest
        return _cmd_uninstall(purge=purge)

    print(f"Unknown command: {sub}", file=sys.stderr)
    _print_help(out=sys.stderr)
    return 2


def _print_help(out=None) -> None:
    out = out or sys.stdout
    print(
        "Usage: solomon <command> [args]\n\n"
        "Commands:\n"
        "  init             Scaffold ~/.hermes/solomon/ and finish setup.\n"
        "  doctor           Check that everything is wired right.\n"
        "  logs             View structured logs.\n"
        "                   Flags: --errors, --today, --since DATE,\n"
        "                          --grep PATTERN, --event NAME, --follow.\n"
        "  register-crons   Register the 17 Solomon cron jobs with Hermes.\n"
        "  uninstall-crons  Remove every Solomon cron job from Hermes.\n"
        "  daily            Fire the daily reflection cron now.\n"
        "  weekly [name]    Fire weekly compression jobs.\n"
        "                   Default: all. Pass a playbook name to compress one,\n"
        "                   or 'summary' for the profile-summary job.\n"
        "  checkin          Fire the weekly check-in cron now.\n"
        "  ingest           Process anything in ~/.hermes/solomon/inbox/ now.\n"
        "  uninstall        Restore the pre-Solomon Hermes config.\n"
        "                   Use --purge to also delete ~/.hermes/solomon/.\n",
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
    print("  Or run `solomon doctor` to confirm everything is wired right.")
    return 0


# ---------------------------------------------------------------------------
# register-crons / uninstall-crons
# ---------------------------------------------------------------------------


def _build_adapter():
    """Construct a HermesAdapter without a plugin ctx for CLI use.

    Methods that read disk or call Hermes APIs work fine; methods that
    register tools/commands/hooks would fail (no ctx), but those aren't
    called from the CLI cron-management path.
    """
    from .adapter import HermesAdapter
    return HermesAdapter(ctx=None)


def _cmd_register_crons() -> int:
    from . import checkin, daily, weekly
    a = _build_adapter()
    try:
        daily.register(a)
        weekly_jobs = weekly.register(a)
        checkin.register(a)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ Could not register cron jobs: {e}", file=sys.stderr)
        print("    Hermes may not be installed, or its cron module may be unavailable.", file=sys.stderr)
        return 1
    print(f"  ✓ Daily reflection registered (02:00 nightly)")
    print(f"  ✓ {len(weekly_jobs) - 1} weekly compression jobs registered "
          "(Sunday 03:00–04:05, staggered)")
    print(f"  ✓ Profile summary regeneration registered (Sunday 04:10)")
    print(f"  ✓ Weekly check-in registered (Friday 15:00)")
    print()
    print("Total: 17 Hermes cron jobs.")
    return 0


def _cmd_uninstall_crons() -> int:
    from . import checkin, daily, weekly
    a = _build_adapter()
    removed = 0
    if daily.unregister(a):
        removed += 1
    removed += weekly.unregister(a)
    if checkin.unregister(a):
        removed += 1
    print(f"  ✓ Removed {removed} Solomon cron jobs from Hermes.")
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


def _cmd_uninstall(purge: bool = False) -> int:
    import shutil
    from .adapter import hermes_config_path, hermes_skills_dir_for

    # 1. Remove Solomon's Hermes cron jobs.
    print("Removing Solomon's cron jobs from Hermes...")
    _cmd_uninstall_crons()

    # 2. Restore Hermes config from the pre-Solomon backup, if one exists.
    cfg = hermes_config_path()
    backup = cfg.with_suffix(".yaml.pre-solomon")
    if backup.exists():
        shutil.copy2(backup, cfg)
        print(f"  ✓ Restored {cfg} from {backup}")
    else:
        # Fall back to disabling via the Hermes CLI.
        a = _build_adapter()
        if a.disable_plugin("solomon"):
            print("  ✓ Disabled solomon via `hermes plugins disable`")
        else:
            print("  (no Hermes-config backup; couldn't auto-disable. "
                  "Run `hermes plugins disable solomon` manually if needed.)")

    # 3. Remove the skill files.
    skill_dir = hermes_skills_dir_for("solomon")
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
        print(f"  ✓ Removed skill files at {skill_dir}")

    # 4. Optionally purge the data folder.
    from . import profile
    home = profile.home()
    if purge:
        if home.exists():
            try:
                response = input(
                    f"This will permanently delete everything Solomon knows about you at\n"
                    f"  {home}\n"
                    f"Type 'yes' to confirm: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                response = ""
            if response == "yes":
                shutil.rmtree(home)
                print(f"  ✓ Deleted {home}")
            else:
                print(f"  (kept {home})")
    else:
        print()
        print(f"Your Solomon data is at {home}.")
        print("Delete that folder for a clean slate, or re-run with --purge "
              "to do it for you (after a confirmation prompt).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Solomon command-line interface.

Exposes:
  solomon init       — provision database, run first-time setup
  solomon onboard    — run an onboarding session (or `list`)
  solomon doctor     — health check
  solomon sleep      — run the nightly cycle on demand
  solomon uninstall  — restore pre-Solomon Hermes config

Entry point declared in pyproject.toml as ``solomon = solomon.cli:main``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

try:
    import click
    from rich.console import Console
    from rich.panel import Panel
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False
    click = None  # type: ignore[assignment]
    Console = None  # type: ignore[assignment]

DEFAULT_DATABASE_URL = "postgresql://solomon:solomon@localhost:5432/solomon"

# Where Hermes lives. This is the standard install path used by the
# Hermes installer; ~/.hermes is for user data, the package itself is
# in /usr/local/lib or wherever pip put it.
HERMES_HOME = Path(os.path.expanduser(os.getenv("HERMES_HOME", "~/.hermes")))


def _print(msg: str, style: str = "") -> None:
    if HAVE_RICH:
        Console().print(msg, style=style)
    else:
        print(msg)


def cmd_init() -> int:
    """Provision Solomon storage, scaffold the foundation directory,
    register Solomon with Hermes if needed.
    """
    _print("[bold cyan]Solomon — first-time setup[/]", style="cyan")

    # Step 1: scaffold ~/.hermes/solomon/
    base = HERMES_HOME / "solomon"
    for sub in ("foundation", "taxonomy", "logs", "data"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    _print(f"✓ Solomon home: {base}")

    # Step 2: detect or prompt for the database.
    db_url = os.getenv("SOLOMON_DATABASE_URL", "").strip()
    if not db_url:
        _print(
            "\nNo SOLOMON_DATABASE_URL set. Solomon needs Postgres with pgvector.\n"
            "Pick one:\n"
            "  1. Local Postgres via Docker (we'll start it for you)\n"
            "  2. Bring your own Postgres URL (Supabase, RDS, self-hosted)\n"
        )
        try:
            choice = input("Choice [1/2]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
        if choice == "2":
            db_url = input("Postgres URL: ").strip()
        else:
            db_url = _provision_local_postgres()
            if not db_url:
                _print("✗ Local Postgres setup failed.", style="red")
                return 1

    # Step 3: persist the URL to ~/.hermes/.env so Hermes child processes inherit it.
    env_path = HERMES_HOME / ".env"
    _upsert_env(env_path, "SOLOMON_DATABASE_URL", db_url)
    os.environ["SOLOMON_DATABASE_URL"] = db_url

    # Step 4: run schema migrations.
    _print("\n[cyan]→ Initializing schema...[/]")
    try:
        from .storage.pool import init_storage

        class _StandaloneAdapter:
            def get_config(self, key, default=None): return default

        init_storage(_StandaloneAdapter())
        _print("✓ Schema ready.")
    except Exception as e:  # noqa: BLE001
        _print(f"✗ Schema init failed: {e}", style="red")
        return 1

    # Step 5: install cron jobs.
    _install_cron_jobs()

    # Step 6: enable the Solomon plugin in Hermes config.
    _enable_in_hermes_config()

    _print(
        "\n[bold green]Solomon is ready.[/]\n\n"
        "Next steps:\n"
        "  1. Run [cyan]solomon onboard session_1[/] to start the foundation interview.\n"
        "  2. Or jump straight in: [cyan]hermes[/] — every conversation will now flow through Solomon.\n"
        "     Use [cyan]/private[/] in any session to opt out of logging for that conversation.\n",
        style="green",
    )
    return 0


def cmd_doctor() -> int:
    """Run health checks."""
    _print("[bold cyan]Solomon doctor[/]")
    ok = True

    # Storage check
    try:
        from .storage.pool import init_storage

        class _StandaloneAdapter:
            def get_config(self, key, default=None): return default

        init_storage(_StandaloneAdapter())
        _print("✓ Database connection OK")
    except Exception as e:  # noqa: BLE001
        _print(f"✗ Database: {e}", style="red")
        ok = False

    # LLM check
    from .reasoning.llm import get_client
    client = get_client()
    if client.configured:
        _print(f"✓ LLM configured ({client._model_fast} / {client._model_deep})")
    else:
        _print(
            "✗ No LLM provider configured. Set OPENROUTER_API_KEY (recommended) "
            "or SOLOMON_LLM_BASE_URL + SOLOMON_LLM_API_KEY.",
            style="red",
        )
        ok = False

    # Hermes plugin entry
    try:
        import importlib.metadata
        eps = importlib.metadata.entry_points()
        group = list(eps.select(group="hermes_agent.plugins")) if hasattr(eps, "select") else eps.get("hermes_agent.plugins", [])  # type: ignore[union-attr]
        names = [ep.name for ep in group]
        if "solomon" in names:
            _print("✓ Solomon registered as Hermes plugin")
        else:
            _print("? Solomon entry point not found. Run `pip install -e .` from the solomon repo.")
    except Exception as e:  # noqa: BLE001
        _print(f"? Plugin check skipped: {e}")

    return 0 if ok else 1


def cmd_onboard(args: List[str]) -> int:
    from .onboarding.session_runner import main as onboard_main
    return onboard_main(args)


def cmd_sleep() -> int:
    from .sleep.runner import main as sleep_main
    return sleep_main()


def cmd_uninstall() -> int:
    _print("[bold yellow]Solomon uninstall — restores pre-Solomon Hermes config.[/]")
    backup = HERMES_HOME / "config.yaml.pre-solomon"
    cfg = HERMES_HOME / "config.yaml"
    if backup.exists():
        shutil.copy2(backup, cfg)
        _print(f"✓ Restored {cfg} from {backup}")
    else:
        _print("(no backup found; nothing restored)")
    _print("Postgres data is left in place. Drop the database manually if you want a clean slate.")
    return 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _provision_local_postgres() -> str:
    """Start a local Postgres+pgvector container via Docker."""
    container = "solomon-pg"
    image = "pgvector/pgvector:pg16"
    port = "5432"
    if shutil.which("docker") is None:
        _print("✗ Docker not found. Install Docker first, or use option 2 (BYO Postgres URL).", style="red")
        return ""
    # Check if container already exists.
    r = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True)
    if container in r.stdout.splitlines():
        subprocess.run(["docker", "start", container], capture_output=True)
    else:
        subprocess.run(
            ["docker", "run", "-d", "--name", container,
             "-e", "POSTGRES_USER=solomon",
             "-e", "POSTGRES_PASSWORD=solomon",
             "-e", "POSTGRES_DB=solomon",
             "-p", f"{port}:5432",
             image],
            capture_output=True,
        )
    return f"postgresql://solomon:solomon@localhost:{port}/solomon"


def _upsert_env(env_path: Path, key: str, value: str) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    new_lines: List[str] = []
    written = False
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            written = True
        else:
            new_lines.append(line)
    if not written:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _enable_in_hermes_config() -> None:
    """Add ``solomon`` to ``plugins.enabled`` in ~/.hermes/config.yaml.

    Idempotent. Creates a backup at config.yaml.pre-solomon the first time.
    """
    cfg_path = HERMES_HOME / "config.yaml"
    if not cfg_path.exists():
        # Hermes hasn't been run yet. The plugin entry point will still be
        # picked up by Hermes pip discovery, so this is a no-op.
        return
    backup = HERMES_HOME / "config.yaml.pre-solomon"
    if not backup.exists():
        shutil.copy2(cfg_path, backup)
    try:
        import yaml
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        return
    plugins = cfg.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    if "solomon" not in enabled:
        enabled.append("solomon")
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)
        _print("✓ Solomon enabled in ~/.hermes/config.yaml")


def _install_cron_jobs() -> None:
    """Register the sleep cycle and prediction checker with Hermes cron."""
    # The Hermes cron CLI is `hermes cron create ...`. We try it; if it
    # fails (e.g. Hermes not on PATH), we leave instructions for the user.
    sleep_cmd = (
        "python -m solomon.sleep.runner"
    )
    try:
        subprocess.run(
            ["hermes", "cron", "create", "0 2 * * *", "--prompt", sleep_cmd, "--name", "solomon-sleep-cycle"],
            capture_output=True, timeout=20,
        )
        _print("✓ Sleep cycle cron installed (02:00 nightly).")
    except Exception as e:  # noqa: BLE001
        _print(
            f"? Could not install cron via hermes CLI ({e}). "
            "Run manually: hermes cron create '0 2 * * *' --prompt 'python -m solomon.sleep.runner' --name solomon-sleep-cycle",
        )


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv or sys.argv[1:])
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    return {
        "init": lambda: cmd_init(),
        "doctor": lambda: cmd_doctor(),
        "onboard": lambda: cmd_onboard(rest),
        "sleep": lambda: cmd_sleep(),
        "uninstall": lambda: cmd_uninstall(),
    }.get(cmd, lambda: (_print(f"Unknown command: {cmd}", style="red") or 1))()  # type: ignore[func-returns-value]


if __name__ == "__main__":
    raise SystemExit(main())

"""Solomon command-line interface.

Exposes:
  solomon init                 — provision database, run first-time setup
  solomon onboard              — run an onboarding session (or `list`); also drives ingestion
  solomon ingest PATH [PATH...] — ingest one or more historical documents
  solomon ingestion review     — review extracted decisions and proposed heuristics
  solomon ingestion list       — list ingestion jobs
  solomon corpus ingest PATH   — run the corpus pipeline on one or more files
  solomon corpus watch         — start the inbox watcher (long-lived)
  solomon corpus stats         — print manifest + embeddings counts
  solomon corpus forget        — owner-initiated deletion cascade
  solomon corpus lint          — health checks
  solomon doctor               — health check
  solomon sleep                — run the nightly cycle on demand
  solomon uninstall            — restore pre-Solomon Hermes config

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


def cmd_corpus(args: List[str]) -> int:
    """Corpus pipeline subcommands.

    Usage:
        solomon corpus ingest <path> [<path>...]
        solomon corpus watch                 — start the inbox watcher (blocking)
        solomon corpus stats                 — manifest + embeddings counters
        solomon corpus forget --sha SHA      — owner-forget cascade
        solomon corpus forget --path REL     — owner-forget cascade by raw_path
        solomon corpus lint                  — run all health checks
    """
    if not args:
        _print(
            "Usage: solomon corpus <ingest|watch|stats|forget|lint> ...",
            style="yellow",
        )
        return 1
    sub, rest = args[0], args[1:]
    if sub == "ingest":
        return _corpus_ingest(rest)
    if sub == "watch":
        return _corpus_watch()
    if sub == "stats":
        return _corpus_stats()
    if sub == "forget":
        return _corpus_forget(rest)
    if sub == "lint":
        return _corpus_lint()
    _print(f"Unknown subcommand: corpus {sub}", style="red")
    return 1


def _corpus_ingest(paths: List[str]) -> int:
    if not paths:
        _print("Usage: solomon corpus ingest <path> [<path>...]", style="yellow")
        return 1
    from .corpus.ingest import ingest_directory, ingest_file
    summary = {"success": 0, "partial": 0, "failed": 0, "skipped": 0, "parked": 0}
    total_vectors = 0
    total_rules = 0
    total_wiki = 0
    for raw in paths:
        p = Path(os.path.expanduser(raw))
        if p.is_dir():
            results = ingest_directory(p)
        else:
            results = [ingest_file(p)]
        for r in results:
            summary[r.status] = summary.get(r.status, 0) + 1
            total_vectors += r.vector_count
            total_rules += r.rules_written
            total_wiki += len(r.wiki_pages)
            label = f"  [{r.status}]"
            details = f"{p.name if not p.is_dir() else (r.raw_path or '?')}"
            if r.reason:
                details += f" ({r.reason})"
            _print(f"{label} {details}")
    _print(
        f"\n[green]Done.[/] success={summary['success']} "
        f"partial={summary['partial']} skipped={summary['skipped']} "
        f"parked={summary['parked']} failed={summary['failed']} "
        f"| {total_vectors} embeddings, {total_wiki} wiki pages, "
        f"{total_rules} proposed rules"
    )
    if summary.get("failed", 0) or summary.get("partial", 0):
        _print(
            "\nReview the proposed rules with: [cyan]solomon mentoring review[/]",
            style="yellow",
        )
    return 0


def _corpus_watch() -> int:
    from .workers.corpus_inbox_watcher import main as watcher_main
    _print("[cyan]Starting corpus inbox watcher (Ctrl-C to stop)...[/]")
    return int(watcher_main() or 0)


def _corpus_stats() -> int:
    from .corpus import manifest as cm
    from .corpus import embed as ce
    from .corpus import rules as cr
    stats = cm.stats()
    _print("[bold cyan]Corpus stats[/]")
    _print(f"  files: total={stats.get('total', 0)}  success={stats.get('success', 0)}  "
           f"pending={stats.get('pending', 0)}  partial={stats.get('partial', 0)}  "
           f"failed={stats.get('failed', 0)}  forgotten={stats.get('forgotten', 0)}")
    _print(f"  embeddings: corpus_raw={ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_RAW)}  "
           f"corpus_wiki={ce.count_for_source_table(ce.SOURCE_TABLE_CORPUS_WIKI)}")
    _print(f"  proposed_rules queued: {len(cr.list_queued())}")
    return 0


def _corpus_forget(args: List[str]) -> int:
    sha = None
    raw_path = None
    file_id = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--sha" and i + 1 < len(args):
            sha = args[i + 1]; i += 2; continue
        if a == "--path" and i + 1 < len(args):
            raw_path = args[i + 1]; i += 2; continue
        if a == "--id" and i + 1 < len(args):
            file_id = args[i + 1]; i += 2; continue
        i += 1
    if not (sha or raw_path or file_id):
        _print("Usage: solomon corpus forget --sha SHA | --path REL | --id FILE_ID",
               style="yellow")
        return 1
    from .corpus.forget import forget_file
    s = forget_file(sha256=sha, raw_path=raw_path, file_id=file_id)
    if not s["found"]:
        _print("File not found in ingested_files.", style="yellow")
        return 1
    _print(
        f"[green]Forgotten.[/] file_id={s['file_id']} raw_path={s['raw_path']} "
        f"embeddings_deleted={s['embeddings_deleted']} rules_deleted={s['rules_deleted']} "
        f"disk_deleted={s['disk_deleted']}"
    )
    return 0


def _corpus_lint() -> int:
    from .corpus.lint import run_lint, summary
    findings = run_lint()
    summ = summary(findings)
    _print(f"[bold cyan]Corpus lint[/]: {summ.get('total', 0)} findings "
           f"({summ.get('errors', 0)} errors, {summ.get('warnings', 0)} warnings)")
    for f in findings:
        prefix = "[red]ERROR[/]" if f.severity == "error" else "[yellow]WARN[/]"
        _print(f"  {prefix} {f.code}: {f.detail}")
    return 1 if summ.get("errors", 0) else 0


def cmd_ingest(args: List[str]) -> int:
    """Ingest one or more historical documents.

    Usage:
        solomon ingest path/to/file1.txt path/to/file2.eml ...
        solomon ingest --flag-sensitive path/to/medical.pdf -- path/to/other.txt
    """
    if not args:
        _print("Usage: solomon ingest [--flag-sensitive PATH ...] PATH [PATH ...]", style="yellow")
        return 1
    flagged: List[str] = []
    paths: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--flag-sensitive" and i + 1 < len(args):
            flagged.append(args[i + 1])
            i += 2
            continue
        if a == "--":
            i += 1
            continue
        paths.append(a)
        i += 1
    paths = [p for p in paths if p not in set(flagged) or True]  # keep them; flagged ones get the skip-flag
    if not paths and not flagged:
        _print("No paths provided.", style="yellow")
        return 1
    all_paths = paths + flagged
    _print(f"[cyan]Ingesting {len(all_paths)} document(s)...[/]")
    from .ingestion.upload_handler import ingest_paths
    summary = ingest_paths(all_paths, flagged_sensitive_paths=flagged)
    _print(
        f"\n[green]Done.[/] Processed {summary['documents_processed']}, "
        f"skipped {summary['documents_skipped']}, "
        f"extracted {summary['decisions_extracted']} decisions, "
        f"stored {summary['embeddings_stored']} embeddings, "
        f"proposed {summary['heuristics_proposed']} heuristics."
    )
    if summary["errors"]:
        _print(f"[yellow]Errors:[/] {summary['errors']}", style="yellow")
    _print("\nReview the proposed heuristics with: [cyan]solomon ingestion review[/]")
    return 0


def cmd_ingestion(args: List[str]) -> int:
    """Manage ingestion: list jobs, review pending heuristics + decisions."""
    if not args or args[0] == "list":
        from .ingestion import list_pending_jobs
        from .storage.decisions import get_or_create_tenant_id
        tenant_id = get_or_create_tenant_id()
        jobs = list_pending_jobs(tenant_id)
        if not jobs:
            _print("No pending ingestion jobs.")
            return 0
        for j in jobs:
            _print(f"  job {j['job_id']}: {j['status']} ({j['document_count']} docs, created {j['created_at']})")
        return 0
    if args[0] == "review":
        return _interactive_review()
    _print(f"Unknown subcommand: {args[0]}. Try `list` or `review`.", style="red")
    return 1


def _interactive_review() -> int:
    """Walk the owner through pending heuristics and high-salience decisions."""
    from .ingestion.review_queue import (
        pending_review_items,
        approve_heuristic,
        reject_heuristic,
        defer_heuristic,
    )
    from .storage.decisions import get_or_create_tenant_id
    tenant_id = get_or_create_tenant_id()
    items = pending_review_items(tenant_id)
    heuristics = items.get("heuristics", [])
    decisions = items.get("decisions", [])

    if not heuristics and not decisions:
        _print("[green]Nothing pending review. Inbox zero.[/]", style="green")
        return 0

    _print(f"\n[bold cyan]Solomon review queue[/]: {len(heuristics)} heuristic proposals, {len(decisions)} extracted decisions.\n")

    # Heuristics first — they're the more valuable thing to look at.
    for h in heuristics:
        _print("\n" + "─" * 60)
        _print(f"[bold]Proposed heuristic[/] (id={h.get('pending_id')})")
        _print(f"  scope:     {h.get('scope')}")
        _print(f"  condition: {h.get('proposed_condition')}")
        _print(f"  action:    {h.get('proposed_action')}")
        _print(f"  support:   {h.get('support_count')} decisions back this up")
        try:
            choice = input("\n  [a]pprove  [r]eject  [d]efer  [s]kip > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice == "a":
            new_id = approve_heuristic(tenant_id, int(h["pending_id"]))
            _print(f"  ✓ Approved as heuristic {new_id}")
        elif choice == "r":
            reject_heuristic(tenant_id, int(h["pending_id"]))
            _print("  ✗ Rejected")
        elif choice == "d":
            defer_heuristic(tenant_id, int(h["pending_id"]))
            _print("  ⏸ Deferred")
        # 's' or anything else = skip

    _print("\n[green]Review complete.[/]", style="green")
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
        "ingest": lambda: cmd_ingest(rest),
        "ingestion": lambda: cmd_ingestion(rest),
        "corpus": lambda: cmd_corpus(rest),
        "sleep": lambda: cmd_sleep(),
        "uninstall": lambda: cmd_uninstall(),
    }.get(cmd, lambda: (_print(f"Unknown command: {cmd}", style="red") or 1))()  # type: ignore[func-returns-value]


if __name__ == "__main__":
    raise SystemExit(main())

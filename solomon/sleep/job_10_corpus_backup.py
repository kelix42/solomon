"""Job 10 — Corpus backup.

Nightly tarball of the corpus tree (``raw/`` + ``wiki/``) so a botched
ingestion / lint / forget can be rolled back from disk. Output lands in
``<corpus_root>/../backups/<iso_timestamp>.tar.gz``.

Defaults are ``~/.hermes/solomon/corpus/`` for the source and
``~/.hermes/solomon/backups/`` for the destination. ``SOLOMON_CORPUS_ROOT``
overrides the source path (mirrors what the inbox watcher already
honours via ``solomon.corpus.schema_config.corpus_root``).

Retention: any ``*.tar.gz`` in the backups dir whose mtime is older
than 30 days is deleted after the new backup lands. Setting the
``SOLOMON_BACKUP_RETENTION_DAYS`` env var changes that window (used by
tests; not part of the documented surface yet).

Idempotency: running the job twice in a row produces two distinct
backup files because the filename embeds a UTC timestamp at second
resolution — but the second tarball just snapshots the same tree
again; nothing changes on disk apart from a new file. Subsequent runs
within the same second reuse the existing tarball by short-circuiting.
"""

from __future__ import annotations

import logging
import os
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("solomon.sleep.job_10")


_DEFAULT_RETENTION_DAYS = 30


def _retention_days() -> int:
    raw = os.getenv("SOLOMON_BACKUP_RETENTION_DAYS", "").strip()
    if not raw:
        return _DEFAULT_RETENTION_DAYS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "Invalid SOLOMON_BACKUP_RETENTION_DAYS=%r — falling back to %d",
            raw, _DEFAULT_RETENTION_DAYS,
        )
        return _DEFAULT_RETENTION_DAYS


def _source_root() -> Path:
    """Active corpus root — same resolution as the inbox watcher."""
    from ..corpus.schema_config import corpus_root
    return corpus_root()


def _backup_dir(corpus_root: Path) -> Path:
    return corpus_root.parent / "backups"


def _prune_old_backups(backup_dir: Path, max_age_days: int) -> int:
    """Delete .tar.gz files whose mtime is older than max_age_days. Returns count."""
    if not backup_dir.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    deleted = 0
    for entry in backup_dir.iterdir():
        if not entry.is_file():
            continue
        if not entry.name.endswith(".tar.gz"):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            try:
                entry.unlink()
                deleted += 1
            except OSError as e:
                logger.warning("Failed to prune old backup %s: %s", entry, e)
    return deleted


def run(*, tenant_id: str, **kwargs: Any) -> Dict[str, Any]:
    """Snapshot the corpus tree to a timestamped tarball + prune old backups."""
    source_root = _source_root()
    backup_dir = _backup_dir(source_root)

    raw_dir = source_root / "raw"
    wiki_dir = source_root / "wiki"

    sources: List[Path] = [d for d in (raw_dir, wiki_dir) if d.exists()]
    if not sources:
        logger.info(
            "Job 10 corpus backup: source root %s has no raw/ or wiki/ — skipping",
            source_root,
        )
        return {
            "items_processed": 0,
            "tarball": None,
            "entries": 0,
            "pruned": 0,
            "tokens": 0,
        }

    backup_dir.mkdir(parents=True, exist_ok=True)
    # ISO timestamp, second resolution, safe for filenames.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tarball = backup_dir / f"{ts}.tar.gz"

    if tarball.exists():
        # Same-second re-invocation — keep idempotency by re-using the file.
        logger.info("Job 10 corpus backup: %s already exists, skipping write", tarball)
    else:
        entries = 0
        try:
            with tarfile.open(tarball, "w:gz") as tar:
                for src in sources:
                    # Use the directory name as the archive arcname so the
                    # tarball contains ``raw/...`` and ``wiki/...`` rather
                    # than absolute paths.
                    tar.add(src, arcname=src.name)
                    # Count entries we just added (everything under src).
                    for _ in src.rglob("*"):
                        entries += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("Job 10 corpus backup failed at write: %s", e)
            # Clean up a partial tarball so the next run starts fresh.
            try:
                tarball.unlink()
            except OSError:
                pass
            return {
                "items_processed": 0,
                "tarball": None,
                "entries": 0,
                "pruned": 0,
                "tokens": 0,
            }
        logger.info(
            "Job 10 corpus backup: wrote %s (%d entries from %d trees)",
            tarball, entries, len(sources),
        )

    pruned = _prune_old_backups(backup_dir, _retention_days())
    if pruned:
        logger.info("Job 10 corpus backup: pruned %d old backups (>%dd)",
                    pruned, _retention_days())

    # Re-count entries from the (potentially pre-existing) tarball for the result.
    try:
        with tarfile.open(tarball, "r:gz") as tar:
            entries = len(tar.getnames())
    except Exception:  # noqa: BLE001
        entries = 0

    return {
        "items_processed": entries,
        "tarball": str(tarball),
        "entries": entries,
        "pruned": pruned,
        "tokens": 0,
    }

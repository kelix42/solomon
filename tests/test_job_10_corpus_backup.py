"""Tests for solomon.sleep.job_10_corpus_backup."""

from __future__ import annotations

import os
import tarfile
import time
from pathlib import Path

import pytest

from solomon.sleep import job_10_corpus_backup as J


def _seed_corpus(tmp_path) -> Path:
    """Build a minimal corpus/raw/ + corpus/wiki/ tree and return the root."""
    root = tmp_path / "corpus"
    raw = root / "raw" / "docs"
    raw.mkdir(parents=True)
    (raw / "intake.txt").write_text("hello intake")
    (raw / "notes.md").write_text("# notes\nbody\n")
    wiki = root / "wiki"
    wiki.mkdir()
    (wiki / "people.md").write_text("# people\n")
    return root


def test_job_10_writes_tarball_with_expected_entries(tmp_path, monkeypatch):
    root = _seed_corpus(tmp_path)
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(root))

    result = J.run(tenant_id="default")

    tarball = Path(result["tarball"])
    assert tarball.exists()
    assert tarball.parent == root.parent / "backups"

    with tarfile.open(tarball, "r:gz") as tar:
        names = set(tar.getnames())
    # arcname stripping puts the subtree under raw/... and wiki/...
    assert any(n.startswith("raw") and "intake.txt" in n for n in names)
    assert any(n.startswith("raw") and "notes.md" in n for n in names)
    assert any(n.startswith("wiki") and "people.md" in n for n in names)


def test_job_10_skips_when_no_raw_or_wiki(tmp_path, monkeypatch):
    root = tmp_path / "empty-corpus"
    root.mkdir()
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(root))

    result = J.run(tenant_id="default")

    assert result["tarball"] is None
    assert result["entries"] == 0
    assert not (root.parent / "backups").exists()


def test_job_10_retention_deletes_old_backups(tmp_path, monkeypatch):
    root = _seed_corpus(tmp_path)
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(root))
    backup_dir = root.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Create an artificially-old backup file and backdate its mtime to 60 days ago.
    old = backup_dir / "20200101T000000Z.tar.gz"
    old.write_bytes(b"\x1f\x8b\x08\x00")  # not a valid gzip body but mtime is what matters
    sixty_days_ago = time.time() - 60 * 86400
    os.utime(old, (sixty_days_ago, sixty_days_ago))

    # And a recent one we expect to keep.
    recent = backup_dir / "recent.tar.gz"
    recent.write_bytes(b"\x1f\x8b\x08\x00")
    one_day_ago = time.time() - 86400
    os.utime(recent, (one_day_ago, one_day_ago))

    result = J.run(tenant_id="default")

    assert not old.exists(), "60-day-old backup should be pruned"
    assert recent.exists(), "1-day-old backup should be kept"
    assert result["pruned"] >= 1


def test_job_10_retention_window_configurable(tmp_path, monkeypatch):
    root = _seed_corpus(tmp_path)
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(root))
    monkeypatch.setenv("SOLOMON_BACKUP_RETENTION_DAYS", "1")
    backup_dir = root.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # 2-day-old backup; with retention=1 it must be pruned.
    old = backup_dir / "20240101T000000Z.tar.gz"
    old.write_bytes(b"x")
    two_days_ago = time.time() - 2 * 86400
    os.utime(old, (two_days_ago, two_days_ago))

    J.run(tenant_id="default")

    assert not old.exists()


def test_job_10_is_idempotent(tmp_path, monkeypatch):
    """Two back-to-back runs must not crash; same-second runs reuse the tarball."""
    root = _seed_corpus(tmp_path)
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(root))

    r1 = J.run(tenant_id="default")
    r2 = J.run(tenant_id="default")

    assert Path(r1["tarball"]).exists()
    assert Path(r2["tarball"]).exists()
    # At minimum, both runs succeeded and the backups dir holds >=1 tarball.
    backup_dir = root.parent / "backups"
    tarballs = list(backup_dir.glob("*.tar.gz"))
    assert len(tarballs) >= 1

"""Tests for the JSONL logger."""

from __future__ import annotations

import io
import json
from pathlib import Path

from solomon import logs


def test_log_writes_jsonl(solomon_home: Path):
    logs.log("turn_start", scope="customer_pricing", session_id="s1")
    assert logs.log_path().exists()
    lines = logs.log_path().read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "turn_start"
    assert entry["scope"] == "customer_pricing"
    assert entry["session_id"] == "s1"
    assert entry["level"] == "INFO"
    assert "ts" in entry


def test_log_error_includes_stack(solomon_home: Path):
    try:
        raise ValueError("boom")
    except ValueError as e:
        logs.log_error("error", e, where="test.py:1")
    entry = json.loads(logs.log_path().read_text().strip().splitlines()[0])
    assert entry["level"] == "ERROR"
    assert entry["exc_type"] == "ValueError"
    assert entry["error_msg"] == "boom"
    assert "Traceback" in entry["stack"]


def test_log_unknown_field_passed_through_context(solomon_home: Path):
    logs.log("custom_event", context={"foo": "bar"})
    entry = json.loads(logs.log_path().read_text().strip().splitlines()[0])
    assert entry["context"] == {"foo": "bar"}


def test_view_filters_by_event_and_level(solomon_home: Path):
    logs.log("turn_start", scope="a")
    logs.log("turn_end", scope="a")
    try:
        raise RuntimeError("x")
    except RuntimeError as e:
        logs.log_error("error", e)

    buf = io.StringIO()
    count = logs.view(errors_only=True, out=buf)
    assert count == 1
    assert json.loads(buf.getvalue().strip())["event"] == "error"

    buf = io.StringIO()
    count = logs.view(event="turn_start", out=buf)
    assert count == 1
    assert json.loads(buf.getvalue().strip())["event"] == "turn_start"


def test_view_filters_by_grep(solomon_home: Path):
    logs.log("turn_start", scope="customer_pricing")
    logs.log("turn_start", scope="vendor_negotiation")
    buf = io.StringIO()
    count = logs.view(grep="customer", out=buf)
    assert count == 1


def test_archive_old_logs_creates_tarball(solomon_home: Path):
    import os
    import time

    # Create a "rotated" log file with an old mtime.
    log_dir = solomon_home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    old_file = log_dir / "solomon.2026-01-15.log"
    old_file.write_text('{"ts": "2026-01-15T00:00:00Z", "level": "INFO", "event": "old"}\n')
    # Set mtime to 60 days ago.
    old_time = time.time() - 60 * 86400
    os.utime(old_file, (old_time, old_time))

    count = logs.archive_old_logs(retention_days=30)
    assert count == 1
    assert not old_file.exists()
    assert (solomon_home / "archive" / "logs" / "2026-01.tar.gz").exists()

"""Tests for solomon.sleep.job_12_yaml_reconcile."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml as _yaml

from solomon.sleep import job_12_yaml_reconcile as J
from solomon.storage.pool import cursor, execute, get_conn, parse_json


def _seed_yaml(foundation_dir: Path, name: str, required_fields: dict) -> Path:
    foundation_dir.mkdir(parents=True, exist_ok=True)
    path = foundation_dir / name
    path.write_text(_yaml.safe_dump({
        "last_updated": "2026-05-01T00:00:00Z",
        "required_fields": required_fields,
    }, sort_keys=False))
    return path


def _seed_captured(captured_id: str, statement: str, field_id: str, domain: str = "principles") -> None:
    keywords = json.dumps([f"field:{field_id}"])
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO captured_items "
                "(id, tenant_id, type, domain, statement, verbatim_phrase, example, "
                "keywords, confidence) "
                "VALUES (?, ?, 'principle', ?, ?, ?, '', ?, 'stated')",
                (captured_id, "default", domain, statement, statement, keywords),
            )
        conn.commit()


def _count_drift_queue(tenant_id: str = "default") -> int:
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT COUNT(*) FROM mentoring_queue "
                "WHERE tenant_id=? AND source='yaml_drift'",
                (tenant_id,),
            )
            return int(cur.fetchone()[0])


def test_job_12_detects_drift(solomon_db, tmp_path, monkeypatch):
    monkeypatch.setenv("SOLOMON_FOUNDATION_DIR", str(tmp_path / "foundation"))
    _seed_yaml(tmp_path / "foundation", "03-principles.yaml", {
        "core_promise": {"statement": "deliver in 24 hours", "confidence": "stated"},
    })
    _seed_captured("cap-1", statement="deliver in 48 hours", field_id="core_promise")

    result = J.run(tenant_id="default")

    assert _count_drift_queue() == 1
    assert result["enqueued"] == 1
    assert result["drifts_detected"] == 1

    # Spot-check the payload.
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT priority, payload FROM mentoring_queue "
                "WHERE tenant_id=? AND source='yaml_drift'",
                ("default",),
            )
            row = cur.fetchone()
    assert row[0] == 3
    p = parse_json(row[1])
    assert p["field_id"] == "core_promise"
    assert p["captured_value"] == "deliver in 48 hours"
    assert p["yaml_value"] == "deliver in 24 hours"
    assert "03-principles.yaml" in p["yaml_path"]


def test_job_12_no_drift_when_aligned(solomon_db, tmp_path, monkeypatch):
    monkeypatch.setenv("SOLOMON_FOUNDATION_DIR", str(tmp_path / "foundation"))
    _seed_yaml(tmp_path / "foundation", "03-principles.yaml", {
        "core_promise": {"statement": "deliver in 24 hours"},
    })
    _seed_captured("cap-1", statement="deliver in 24 hours", field_id="core_promise")

    result = J.run(tenant_id="default")

    assert _count_drift_queue() == 0
    assert result["drifts_detected"] == 0


def test_job_12_skips_null_yaml_value(solomon_db, tmp_path, monkeypatch):
    """If the YAML field is null (interview hasn't filled it), don't flag drift."""
    monkeypatch.setenv("SOLOMON_FOUNDATION_DIR", str(tmp_path / "foundation"))
    _seed_yaml(tmp_path / "foundation", "03-principles.yaml", {
        "core_promise": None,
    })
    _seed_captured("cap-1", statement="deliver in 24 hours", field_id="core_promise")

    result = J.run(tenant_id="default")

    assert _count_drift_queue() == 0
    assert result["drifts_detected"] == 0


def test_job_12_is_idempotent(solomon_db, tmp_path, monkeypatch):
    monkeypatch.setenv("SOLOMON_FOUNDATION_DIR", str(tmp_path / "foundation"))
    _seed_yaml(tmp_path / "foundation", "03-principles.yaml", {
        "core_promise": {"statement": "old value"},
    })
    _seed_captured("cap-1", statement="new value", field_id="core_promise")

    J.run(tenant_id="default")
    J.run(tenant_id="default")
    J.run(tenant_id="default")

    assert _count_drift_queue() == 1


def test_job_12_handles_empty_foundation_dir(solomon_db, tmp_path, monkeypatch):
    """No YAMLs → nothing to do, no crash."""
    monkeypatch.setenv("SOLOMON_FOUNDATION_DIR", str(tmp_path / "foundation-empty"))
    _seed_captured("cap-1", statement="x", field_id="anything")

    result = J.run(tenant_id="default")

    assert result["enqueued"] == 0
    assert _count_drift_queue() == 0


def test_job_12_handles_captured_without_field_tag(solomon_db, tmp_path, monkeypatch):
    """captured_items without a field:<id> keyword tag is ignored."""
    monkeypatch.setenv("SOLOMON_FOUNDATION_DIR", str(tmp_path / "foundation"))
    _seed_yaml(tmp_path / "foundation", "03-principles.yaml", {
        "core_promise": {"statement": "24h"},
    })
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO captured_items "
                "(id, tenant_id, type, domain, statement, verbatim_phrase, example, "
                "keywords, confidence) "
                "VALUES (?, ?, 'principle', 'x', ?, ?, '', '[]', 'stated')",
                ("untagged", "default", "anything", "anything"),
            )
        conn.commit()

    result = J.run(tenant_id="default")

    assert _count_drift_queue() == 0
    assert result["enqueued"] == 0


def test_job_12_never_overwrites_yaml(solomon_db, tmp_path, monkeypatch):
    """The yaml file content must be unchanged after the job runs."""
    monkeypatch.setenv("SOLOMON_FOUNDATION_DIR", str(tmp_path / "foundation"))
    yaml_path = _seed_yaml(tmp_path / "foundation", "03-principles.yaml", {
        "core_promise": {"statement": "yaml side"},
    })
    yaml_before = yaml_path.read_text()
    _seed_captured("cap-1", statement="db side", field_id="core_promise")

    J.run(tenant_id="default")

    yaml_after = yaml_path.read_text()
    assert yaml_before == yaml_after

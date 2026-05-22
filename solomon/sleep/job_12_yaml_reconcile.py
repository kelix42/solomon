"""Job 12 — Foundation YAML ↔ captured_items reconcile.

The foundation YAMLs (``foundation/00-industry.yaml`` through
``06-scopes.yaml``) are derived summaries — the canonical store is the
``captured_items`` table. When the owner edits a YAML by hand without
re-running the interview, the DB and the file diverge. This job
catches that drift and surfaces it for human review.

DIFF only, no overwrite. The job never edits the YAML — it only
INSERTs a ``mentoring_queue`` row of source ``'yaml_drift'`` with a
payload describing which field disagrees and the two values. Manual
YAML edits stay in place; the owner decides which side wins during
the next mentoring session.

Drift definition (per REPORT-PIPELINE and the field-tagging convention):
  - For each captured_items row whose ``keywords`` JSON contains
    ``field:<id>`` (the onboarding session-runner tags rows this way),
    locate the foundation YAML whose ``required_fields`` dict has the
    same ``<id>`` key.
  - If the YAML's ``required_fields[<id>].statement`` differs from the
    captured row's ``statement``, that's drift.
  - YAMLs whose ``required_fields`` block is absent or whose entry for
    ``<id>`` is null are treated as "no opinion" — not drift (the
    interview just hasn't filled them yet).

Idempotency: we only enqueue a new drift row if no queued
``yaml_drift`` row already exists with the same ``field_id``. The
LIKE-based check matches the pattern used by job_9.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("solomon.sleep.job_12")


def _foundation_dir() -> Path:
    """Where the foundation YAMLs live.

    Defaults to ``<repo>/foundation``. ``SOLOMON_FOUNDATION_DIR`` env var
    overrides (used by tests).
    """
    env = os.getenv("SOLOMON_FOUNDATION_DIR", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    # Match the conventions used by corpus.schema_config — repo-relative.
    here = Path(__file__).resolve()
    # solomon/sleep/job_12_yaml_reconcile.py → parents[2] is the repo root.
    return here.parents[2] / "foundation"


def _load_yaml(path: Path) -> Optional[Dict[str, Any]]:
    try:
        import yaml  # type: ignore[import-untyped]
    except Exception as e:  # noqa: BLE001
        logger.warning("Job 12: pyyaml not importable: %s", e)
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as e:  # noqa: BLE001
        logger.warning("Job 12: failed to read %s: %s", path, e)
        return None
    return data if isinstance(data, dict) else None


def _build_yaml_index(foundation_dir: Path) -> Dict[str, Tuple[Path, Any]]:
    """Map field_id → (yaml_path, yaml_value).

    Only entries that actually carry a non-null value end up in the
    index — null entries mean "the interview hasn't covered this", not
    drift.
    """
    index: Dict[str, Tuple[Path, Any]] = {}
    if not foundation_dir.exists():
        return index
    for yaml_path in sorted(foundation_dir.glob("*.yaml")):
        data = _load_yaml(yaml_path)
        if not data:
            continue
        required = data.get("required_fields") or {}
        if not isinstance(required, dict):
            continue
        for fid, value in required.items():
            if value is None:
                continue
            # Normalise to the comparable statement string.
            if isinstance(value, dict):
                statement = value.get("statement")
            else:
                statement = value
            if statement is None:
                continue
            index[fid] = (yaml_path, statement)
    return index


def _drift_already_queued(cur: Any, tenant_id: str, field_id: str) -> bool:
    from ..storage.pool import execute
    field_marker = f'"field_id": "{field_id}"'
    execute(
        cur,
        "SELECT 1 FROM mentoring_queue "
        "WHERE tenant_id = ? AND source = 'yaml_drift' AND status = 'queued' "
        "AND payload LIKE ? LIMIT 1",
        (tenant_id, f"%{field_marker}%"),
    )
    return cur.fetchone() is not None


def _iter_field_tagged_captured(
    cur: Any, tenant_id: str,
) -> List[Tuple[str, str, List[str]]]:
    """Return [(captured_id, statement, [field_ids,...]), ...].

    Picks every captured_items row whose keywords JSON contains at
    least one ``field:`` tag. We post-filter for the actual tag in
    Python because the JSON shape (a list) doesn't lend itself to a
    portable WHERE.
    """
    from ..storage.pool import execute, parse_json

    execute(
        cur,
        "SELECT id, statement, keywords FROM captured_items "
        "WHERE tenant_id = ? AND keywords LIKE ?",
        (tenant_id, "%\"field:%"),
    )
    rows = cur.fetchall()
    out: List[Tuple[str, str, List[str]]] = []
    for row in rows:
        cap_id = row[0] if not hasattr(row, "keys") else row["id"]
        stmt = row[1] if not hasattr(row, "keys") else row["statement"]
        kws_raw = row[2] if not hasattr(row, "keys") else row["keywords"]
        kws = parse_json(kws_raw) or []
        if not isinstance(kws, list):
            continue
        field_ids = [
            k.split(":", 1)[1] for k in kws
            if isinstance(k, str) and k.startswith("field:") and ":" in k
        ]
        if field_ids:
            out.append((cap_id, stmt or "", field_ids))
    return out


def run(*, tenant_id: str, **kwargs: Any) -> Dict[str, Any]:
    """Diff foundation YAMLs against captured_items and enqueue drift."""
    from ..storage.pool import cursor, execute, get_conn, jsonify

    foundation_dir = _foundation_dir()
    yaml_index = _build_yaml_index(foundation_dir)
    if not yaml_index:
        logger.info(
            "Job 12 yaml reconcile: no field values in %s — nothing to compare",
            foundation_dir,
        )
        return {
            "items_processed": 0,
            "enqueued": 0,
            "drifts_detected": 0,
            "tokens": 0,
        }

    enqueued = 0
    drifts = 0
    examined = 0

    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                captured_rows = _iter_field_tagged_captured(cur, tenant_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Job 12 yaml reconcile failed at SELECT: %s", e)
        return {"items_processed": 0, "enqueued": 0, "tokens": 0}

    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                for cap_id, captured_value, field_ids in captured_rows:
                    for fid in field_ids:
                        if fid not in yaml_index:
                            continue
                        examined += 1
                        yaml_path, yaml_value = yaml_index[fid]
                        # Normalise both sides for the comparison.
                        cv = (captured_value or "").strip()
                        yv = (str(yaml_value) if yaml_value is not None else "").strip()
                        if cv == yv:
                            continue
                        drifts += 1
                        if _drift_already_queued(cur, tenant_id, fid):
                            continue
                        payload = {
                            "yaml_path": str(yaml_path),
                            "field_id": fid,
                            "captured_id": cap_id,
                            "captured_value": cv,
                            "yaml_value": yv,
                        }
                        execute(
                            cur,
                            "INSERT INTO mentoring_queue "
                            "(tenant_id, source, priority, payload) "
                            "VALUES (?, ?, ?, ?)",
                            (tenant_id, "yaml_drift", 3, jsonify(payload)),
                        )
                        enqueued += 1
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("Job 12 yaml reconcile failed at enqueue: %s", e)

    logger.info(
        "Job 12 yaml reconcile: examined %d captured/yaml pairs, "
        "%d drifts detected, %d newly enqueued",
        examined, drifts, enqueued,
    )
    return {
        "items_processed": examined,
        "enqueued": enqueued,
        "drifts_detected": drifts,
        "tokens": 0,
    }

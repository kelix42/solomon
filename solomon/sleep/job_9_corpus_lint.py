"""Job 9 — Corpus lint.

Runs ``solomon.corpus.lint.run_lint()`` over the active tenant's corpus
state and pushes every ``severity='error'`` finding into the
``mentoring_queue`` so the owner sees it in the next review session.
Warnings are logged-only — the lint module already reports them through
``solomon corpus stats``, no need to flood the queue.

Idempotency: each error becomes a queued row whose payload includes the
finding's ``code`` + ``target``. Before inserting we check whether a row
with the same ``source='lint_finding'`` and a matching ``code``/``target``
in payload is already in status ``queued``; if so we skip. A second
run of the job over the same corpus produces zero new rows.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("solomon.sleep.job_9")


def _payload_already_queued(
    cur: Any,
    tenant_id: str,
    code: str,
    target: Optional[str],
) -> bool:
    """Cheap LIKE-based de-dupe — we only block re-queueing of identical findings."""
    from ..storage.pool import execute

    # We embed both code and target verbatim in the JSON payload, so a
    # substring match on the serialised payload is a safe shortcut.
    code_marker = f'"code": "{code}"'
    if target is None:
        target_marker = '"target": null'
    else:
        target_marker = f'"target": "{target}"'
    execute(
        cur,
        "SELECT 1 FROM mentoring_queue "
        "WHERE tenant_id = ? AND source = 'lint_finding' AND status = 'queued' "
        "AND payload LIKE ? AND payload LIKE ? LIMIT 1",
        (tenant_id, f"%{code_marker}%", f"%{target_marker}%"),
    )
    return cur.fetchone() is not None


def run(*, tenant_id: str, **kwargs: Any) -> Dict[str, Any]:
    """Run corpus lint and enqueue every error finding."""
    from ..corpus.lint import run_lint
    from ..storage.pool import cursor, execute, get_conn, jsonify

    enqueued = 0
    errors_seen = 0
    warnings_seen = 0

    try:
        findings = run_lint(tenant_id=tenant_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Job 9 corpus lint failed at run_lint: %s", e)
        return {"items_processed": 0, "enqueued": 0, "tokens": 0}

    error_findings: List[Any] = []
    for f in findings:
        if getattr(f, "severity", None) == "error":
            errors_seen += 1
            error_findings.append(f)
        else:
            warnings_seen += 1

    if not error_findings:
        logger.info(
            "Job 9 corpus lint: %d findings (0 errors, %d warnings)",
            len(findings), warnings_seen,
        )
        return {
            "items_processed": len(findings),
            "enqueued": 0,
            "errors_seen": errors_seen,
            "warnings_seen": warnings_seen,
            "tokens": 0,
        }

    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                for f in error_findings:
                    code = getattr(f, "code", "unknown")
                    target = getattr(f, "target", None)
                    if _payload_already_queued(cur, tenant_id, code, target):
                        continue
                    payload = {
                        "code": code,
                        "severity": "error",
                        "detail": getattr(f, "detail", ""),
                        "target": target,
                        "metadata": getattr(f, "metadata", {}) or {},
                    }
                    execute(
                        cur,
                        "INSERT INTO mentoring_queue "
                        "(tenant_id, source, priority, payload) "
                        "VALUES (?, ?, ?, ?)",
                        (tenant_id, "lint_finding", 2, jsonify(payload)),
                    )
                    enqueued += 1
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("Job 9 corpus lint failed at enqueue: %s", e)

    logger.info(
        "Job 9 corpus lint: %d findings (%d errors → %d enqueued, %d warnings)",
        len(findings), errors_seen, enqueued, warnings_seen,
    )

    return {
        "items_processed": len(findings),
        "enqueued": enqueued,
        "errors_seen": errors_seen,
        "warnings_seen": warnings_seen,
        "tokens": 0,
    }

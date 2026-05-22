"""The sleep cycle runner.

Runs every night between 2am and 5am local time. Cycles through 8 jobs
in order. Each job runs inside its own try/catch so one failure does not
kill the rest. Per-tenant token budget is debited as we go; if it runs
out, remaining jobs skip LLM calls and run in rule-only mode.

See Part 16 of the design doc for the cycle rules.

Invocation:
  python -m solomon.sleep.runner

Scheduled via solomon/cron/sleep_cycle.cron, installed by `solomon init`
into the Hermes cron scheduler at 02:00 nightly.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("solomon.sleep.runner")


# Order matters. Job 8 depends on signals from 1, 3, 4, 5, 7.
JOB_ORDER: Tuple[Tuple[str, str], ...] = (
    ("hindsight",          "solomon.sleep.job_1_hindsight:run"),
    ("rule_archival",      "solomon.sleep.job_2_archival:run"),
    ("surprise_replay",    "solomon.sleep.job_3_surprise_replay:run"),
    ("stress_test",        "solomon.sleep.job_4_stress_test:run"),
    ("conflict_detection", "solomon.sleep.job_5_conflict:run"),
    ("working_memory",     "solomon.sleep.job_6_working_memory:run"),
    ("autonomy",           "solomon.sleep.job_7_autonomy:run"),
    ("mentoring_scheduler","solomon.sleep.job_8_mentoring_scheduler:run"),
    ("corpus_lint",        "solomon.sleep.job_9_corpus_lint:run"),
    ("corpus_backup",      "solomon.sleep.job_10_corpus_backup:run"),
    ("embed_pending",      "solomon.sleep.job_11_embed_pending:run"),
    ("yaml_reconcile",     "solomon.sleep.job_12_yaml_reconcile:run"),
)


def _load_job(path: str) -> Callable[..., Any]:
    """Resolve 'package.module:fn' into a callable."""
    mod_name, fn_name = path.split(":")
    import importlib
    mod = importlib.import_module(mod_name)
    return getattr(mod, fn_name)


def run_cycle(tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Run the full eight-job cycle for one tenant.

    Returns a per-job summary dict. Always writes one row to cycle_log
    at the end with start/end times and the per-job status.
    """
    from ..storage.pool import get_pool
    from ..storage.decisions import get_or_create_tenant_id

    if tenant_id is None:
        tenant_id = get_or_create_tenant_id()

    started_at = datetime.now(timezone.utc)
    per_job: Dict[str, Dict[str, Any]] = {}
    total_tokens = 0

    for job_name, job_path in JOB_ORDER:
        t0 = time.time()
        status: str = "success"
        items_processed = 0
        tokens = 0
        reason: Optional[str] = None
        try:
            job_fn = _load_job(job_path)
            result = job_fn(tenant_id=tenant_id) or {}
            items_processed = int(result.get("items_processed", 0) or 0)
            tokens = int(result.get("tokens", 0) or 0)
            total_tokens += tokens
        except Exception as e:  # noqa: BLE001
            status = "failed"
            reason = str(e)
            logger.exception("Sleep cycle job %s failed: %s", job_name, e)

        per_job[job_name] = {
            "status": status,
            "items_processed": items_processed,
            "tokens": tokens,
            "duration_s": round(time.time() - t0, 2),
            "reason": reason,
        }

    ended_at = datetime.now(timezone.utc)

    # Persist the cycle log row.
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO cycle_log (tenant_id, started_at, ended_at, total_tokens, per_job) "
                    "VALUES (%s, %s, %s, %s, %s::jsonb);",
                    (tenant_id, started_at, ended_at, total_tokens, json.dumps(per_job)),
                )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to persist cycle_log row: %s", e)

    summary = {
        "tenant_id": tenant_id,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "total_tokens": total_tokens,
        "jobs": per_job,
    }
    logger.info("Sleep cycle complete: %s", summary)
    return summary


def main() -> int:
    """Entry point for `python -m solomon.sleep.runner`."""
    import os
    # Quick storage initialization without a Hermes adapter (we run standalone
    # at 2am, Hermes may not be live).
    from ..storage.pool import init_storage

    class _StandaloneAdapter:
        def get_config(self, key, default=None): return default
        def hermes_logger(self): return logger

    try:
        init_storage(_StandaloneAdapter())
    except Exception as e:  # noqa: BLE001
        logger.error("Sleep cycle could not init storage: %s", e)
        return 1

    tenant_id = os.getenv("SOLOMON_TENANT_ID")
    summary = run_cycle(tenant_id=tenant_id)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

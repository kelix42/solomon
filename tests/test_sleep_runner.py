"""Smoke test for the sleep cycle runner.

Asserts the full ``JOB_ORDER`` walks every entry exactly once per cycle.
We monkey-patch ``_load_job`` so the smoke test stays independent of
each job's real implementation — this is about the runner's
orchestration contract, not the per-job behaviour (those have their
own dedicated test files).
"""

from __future__ import annotations

from collections import Counter

import pytest

from solomon.sleep import runner as R


def test_job_order_lists_all_twelve_jobs():
    names = [n for n, _ in R.JOB_ORDER]
    assert names == [
        "hindsight",
        "rule_archival",
        "surprise_replay",
        "stress_test",
        "conflict_detection",
        "working_memory",
        "autonomy",
        "mentoring_scheduler",
        "corpus_lint",
        "corpus_backup",
        "embed_pending",
        "yaml_reconcile",
    ]


def test_run_cycle_invokes_every_job_exactly_once(solomon_db, monkeypatch):
    """run_cycle must dispatch each JOB_ORDER entry once per cycle."""
    invocations: Counter = Counter()

    def _fake_load_job(path: str):
        def _job(*, tenant_id, **kwargs):
            invocations[path] += 1
            return {"items_processed": 0, "tokens": 0}
        return _job

    monkeypatch.setattr(R, "_load_job", _fake_load_job)

    summary = R.run_cycle(tenant_id="default")

    assert len(invocations) == len(R.JOB_ORDER)
    for _, path in R.JOB_ORDER:
        assert invocations[path] == 1, f"job {path} was invoked {invocations[path]} times"

    # Every job should appear in the per-job summary with status='success'.
    for name, _ in R.JOB_ORDER:
        assert name in summary["jobs"]
        assert summary["jobs"][name]["status"] == "success"


def test_run_cycle_continues_after_a_job_raises(solomon_db, monkeypatch):
    """One job blowing up must not stop the rest of the cycle."""
    invocations: Counter = Counter()

    def _fake_load_job(path: str):
        def _job(*, tenant_id, **kwargs):
            invocations[path] += 1
            if path.endswith("job_5_conflict:run"):
                raise RuntimeError("simulated job_5 failure")
            return {"items_processed": 0, "tokens": 0}
        return _job

    monkeypatch.setattr(R, "_load_job", _fake_load_job)

    summary = R.run_cycle(tenant_id="default")

    # All 12 jobs were attempted.
    assert len(invocations) == len(R.JOB_ORDER)
    # job_5 is marked failed, everything else is success.
    assert summary["jobs"]["conflict_detection"]["status"] == "failed"
    assert "simulated job_5 failure" in (
        summary["jobs"]["conflict_detection"]["reason"] or ""
    )
    for name, _ in R.JOB_ORDER:
        if name == "conflict_detection":
            continue
        assert summary["jobs"][name]["status"] == "success"

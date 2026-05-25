"""Daily reflection cron — Hermes-cron registration and manual-fire.

The cron fires once a day at 02:00 local. The agent turn is driven by
the `solomon-ingest` skill plus this short prompt; the LLM uses the
cron-side tools registered through `tools.register_all` (read_conversations,
list_inbox, archive_file, propose_addition, send_nudge, etc.) to do the
actual work.

This module's job is to make sure the cron is registered and to provide
a manual-fire path used by `/reflect` and by tests.
"""

from __future__ import annotations

from typing import Any, Optional

from . import logs

JOB_NAME = "solomon-daily-reflection"
SCHEDULE = "0 2 * * *"  # daily at 02:00 local

PROMPT = (
    "Run the daily reflection. Use the solomon-ingest skill and these tools.\n"
    "\n"
    "Steps:\n"
    "1. Call read_conversations(since_hours=24) to see yesterday's non-private "
    "    Hermes turns. For each substantial conversation, identify any new "
    "    rules, vocabulary, customer/vendor info, or patterns and call "
    "    propose_addition (or flag_contradiction) per finding.\n"
    "2. Call list_inbox(). For each file: read_inbox_file(name), extract the "
    "    same kinds of findings, then archive_file(name, status='processed'). "
    "    If the LLM call fails for a file, archive with status='failed' and "
    "    an error note.\n"
    "3. Call list_pending_actions_due_for_nudge(). For each due item compose "
    "    a one-sentence nudge in the owner's voice and call send_nudge(item_id, "
    "    text). The send_nudge tool enforces the urgency cadence — don't "
    "    second-guess it.\n"
    "4. Call retry_pending_messages() to flush anything queued from previous "
    "    failed sends.\n"
    "\n"
    "Final response: one-line status summary like 'Processed 3 inbox files, "
    "sent 2 nudges.' Or return exactly [SILENT] if nothing happened."
)


def register(adapter: Any) -> dict:
    """Idempotently register the daily cron with Hermes."""
    job = adapter.register_cron_job(
        name=JOB_NAME,
        schedule=SCHEDULE,
        prompt=PROMPT,
        skill="solomon-ingest",
        deliver="local",
        enabled_toolsets=["solomon"],
    )
    logs.log("cron_registered", context={"job": JOB_NAME, "id": job.get("id")})
    return job


def unregister(adapter: Any) -> bool:
    return adapter.delete_cron_job(JOB_NAME)


def run_now(adapter: Optional[Any] = None) -> dict:
    """Fire the daily cron once, immediately. Used by /reflect.

    Returns whatever Hermes's run_job returns plus a summary dict if
    parseable. If adapter isn't given (e.g., called from a CLI before
    plugin.register), we look it up through tools._adapter.
    """
    if adapter is None:
        from . import tools as tools_mod
        adapter = tools_mod._adapter
    if adapter is None:
        logs.log("run_now_no_adapter", level="WARN", context={"cron": JOB_NAME})
        return {"ok": False, "reason": "no adapter"}
    try:
        from cron import jobs as cron_jobs
        from cron.scheduler import run_job
    except ImportError as e:
        logs.log_error("error", e, where="daily.run_now (import)")
        return {"ok": False, "reason": "Hermes cron unavailable"}
    job = adapter._find_cron_job_by_name(JOB_NAME)
    if not job:
        # Not registered yet — register, then fire.
        job = register(adapter)
    success, output, final_response, error = run_job(job)
    logs.log("cron_fired_manually",
             context={"job": JOB_NAME, "success": success,
                       "error": error or ""})
    return {
        "ok": success,
        "output": output,
        "final_response": final_response,
        "error": error,
        # Backwards-compat keys consumed by slash.cmd_reflect:
        "batches": 0, "files": 0, "proposals": 0,
        "nudges_sent": 0, "actions_stale": 0,
    }


def main() -> int:
    # Kept for `solomon daily` CLI; in practice Hermes's scheduler fires this.
    run_now()
    return 0

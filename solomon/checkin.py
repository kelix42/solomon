"""Weekly check-in cron — LLM-initiated outreach to the owner.

Fires Friday 15:00 local. The agent turn picks one or two genuine gaps
from the profile + queue + recent activity and composes a short message.
The message IS the final response; Hermes auto-delivers via the cron's
`deliver=origin` setting (which falls back to the platform's home channel
for scripted jobs that have no human origin).

If there's genuinely nothing worth surfacing, the LLM returns the
`[SILENT]` marker and Hermes suppresses delivery.
"""

from __future__ import annotations

from typing import Any, Optional

from . import logs

JOB_NAME = "solomon-weekly-checkin"
SCHEDULE = "0 15 * * 5"  # Friday 15:00 local

PROMPT = (
    "Weekly check-in. Use the solomon-interview skill (Mode C — checkin).\n"
    "\n"
    "Steps:\n"
    "1. Call read_profile(...) for the sections most likely to have a gap. "
    "Call read_queue('review') and read_queue('actions') to see unresolved "
    "items. Call read_conversations(since_hours=168) to see last week's "
    "activity.\n"
    "2. Pick ONE or TWO genuine gaps. Examples of what qualifies:\n"
    "   - A required onboarding field marked 'I don't know' that has come "
    "up in recent conversations.\n"
    "   - A contradiction flagged in the queue that hasn't been resolved.\n"
    "   - A playbook section that's thin relative to recent activity in "
    "that area.\n"
    "   - A pattern in last week's conversations not yet captured as a rule.\n"
    "3. Compose ONE short message inviting the owner to talk. Tone: a "
    "thoughtful colleague checking in, not a customer-service bot. No "
    "preamble. Just the message.\n"
    "   - If onboarding isn't finished yet, the genuine gap IS onboarding: "
    "warmly invite them to continue with /onboard. One or two sentences.\n"
    "\n"
    "Your final response IS the literal message delivered to the owner — "
    "nothing else. Do NOT narrate your reasoning, describe the profile state, "
    "or explain why you chose this gap; the owner sees only what you output. "
    "If nothing is worth surfacing this week, respond with exactly [SILENT] "
    "(no other text) and the run is suppressed."
)


def register(adapter: Any) -> dict:
    """Idempotently register the weekly check-in cron."""
    job = adapter.register_cron_job(
        name=JOB_NAME,
        schedule=SCHEDULE,
        prompt=PROMPT,
        skill="solomon-interview",
        deliver="origin",
        enabled_toolsets=["solomon"],
    )
    logs.log("cron_registered", context={"job": JOB_NAME, "id": job.get("id")})
    return job


def unregister(adapter: Any) -> bool:
    return adapter.delete_cron_job(JOB_NAME)


def run_now(adapter: Optional[Any] = None) -> dict:
    """Fire the check-in cron once, immediately. For tests + manual override."""
    if adapter is None:
        from . import tools as tools_mod
        adapter = tools_mod._adapter
    if adapter is None:
        return {"ok": False, "reason": "no adapter"}
    try:
        from cron.scheduler import run_job
    except ImportError as e:
        logs.log_error("error", e, where="checkin.run_now (import)")
        return {"ok": False, "reason": "Hermes cron unavailable"}
    job = adapter._find_cron_job_by_name(JOB_NAME)
    if not job:
        job = register(adapter)
    success, output, final_response, error = run_job(job)
    return {
        "ok": success,
        "final_response": final_response,
        "error": error,
        # Backwards-compat for the v2 callers:
        "sent": success, "queued": False,
        "channel": "origin",
    }


def main() -> int:
    run_now()
    return 0

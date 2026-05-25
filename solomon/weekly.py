"""Weekly compression crons — 14 playbook jobs + 1 summary job.

Per the v3 plan, each playbook is compressed by its own Hermes cron job,
staggered 5 minutes apart on Sunday starting at 03:00. This keeps the
LLM's working context small (one playbook at a time, ~5-10K tokens) and
sidesteps the "lost-in-the-middle" effect that would degrade quality if
all 14 were compressed in a single iterating turn.

Jobs registered:
  solomon-compress-<name>   for each of the 14 playbooks
  solomon-regenerate-summary  at 04:10 Sunday, regenerates profile summary
"""

from __future__ import annotations

from typing import Any, Optional

from . import logs, profile

# Sunday slots in 5-minute increments, starting at 03:00.
def _slot_for_index(i: int) -> str:
    minute = (i * 5) % 60
    hour = 3 + (i * 5) // 60
    return f"{minute} {hour} * * 0"


SUMMARY_JOB_NAME = "solomon-regenerate-summary"
SUMMARY_SCHEDULE = "10 4 * * 0"  # Sunday 04:10


def _job_name_for(playbook: str) -> str:
    return f"solomon-compress-{playbook}"


def _prompt_for(playbook: str) -> str:
    return (
        f"Compress the '{playbook}' playbook. Use the solomon-compress skill.\n"
        "\n"
        f"Steps:\n"
        f"1. Call read_playbook('{playbook}') to see the current content.\n"
        "2. Rewrite it shorter without losing owner-specific phrases or "
        "concrete rules. Strip redundancy and verbose prose. Preserve "
        "verbatim quotes, exact numbers, names, and cross-references.\n"
        f"3. Call propose_compression(file='{playbook}', content=<rewritten>, "
        "summary=<one-sentence what-changed>, diff=''). The owner reviews "
        "the diff in their next /mentor.\n"
        "\n"
        f"Final response: one-line status like 'compressed {playbook}: "
        "removed 3 redundant statements.' Or [SILENT] if the playbook was "
        "already tight and no compression was warranted."
    )


SUMMARY_PROMPT = (
    "Regenerate the owner's profile summary. Use the solomon-compress skill.\n"
    "\n"
    "Steps:\n"
    "1. Call read_profile('industry'), read_profile('why'), "
    "read_profile('principles'), read_profile('non_negotiables') etc. for "
    "every filled section.\n"
    "2. Write a tight ~500-token summary in the owner's voice. Lead with "
    "industry, why, and non-negotiables. Use the owner's exact phrases "
    "where possible. Plain markdown. No section headings.\n"
    "3. Call apply_profile_summary(<text>). This writes immediately; no "
    "owner review.\n"
    "\n"
    "Final response: one line like 'summary regenerated.' Or [SILENT] if "
    "the profile is empty (nothing meaningful to summarize)."
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(adapter: Any) -> list[dict]:
    """Idempotently register all 15 weekly cron jobs.

    Returns the list of registered Hermes cron job dicts.
    """
    registered: list[dict] = []
    for i, playbook in enumerate(profile.PLAYBOOKS):
        job = adapter.register_cron_job(
            name=_job_name_for(playbook),
            schedule=_slot_for_index(i),
            prompt=_prompt_for(playbook),
            skill="solomon-compress",
            deliver="local",
            enabled_toolsets=["solomon"],
        )
        registered.append(job)
    summary_job = adapter.register_cron_job(
        name=SUMMARY_JOB_NAME,
        schedule=SUMMARY_SCHEDULE,
        prompt=SUMMARY_PROMPT,
        skill="solomon-compress",
        deliver="local",
        enabled_toolsets=["solomon"],
    )
    registered.append(summary_job)
    logs.log("cron_registered_set",
             context={"set": "weekly", "count": len(registered)})
    return registered


def unregister(adapter: Any) -> int:
    """Remove all 15 weekly jobs. Returns the count removed."""
    count = 0
    for playbook in profile.PLAYBOOKS:
        if adapter.delete_cron_job(_job_name_for(playbook)):
            count += 1
    if adapter.delete_cron_job(SUMMARY_JOB_NAME):
        count += 1
    return count


def run_now(adapter: Optional[Any] = None,
             which: Optional[str] = None) -> dict:
    """Fire one weekly job ad-hoc, or all of them.

    `which`: a playbook name (compress that one), 'summary' (the summary
    job), 'all' (every weekly job). Default: 'all'.
    """
    if adapter is None:
        from . import tools as tools_mod
        adapter = tools_mod._adapter
    if adapter is None:
        return {"ok": False, "reason": "no adapter"}
    try:
        from cron.scheduler import run_job
    except ImportError as e:
        logs.log_error("error", e, where="weekly.run_now (import)")
        return {"ok": False, "reason": "Hermes cron unavailable"}
    targets: list[str] = []
    if which in (None, "all"):
        targets.extend(_job_name_for(p) for p in profile.PLAYBOOKS)
        targets.append(SUMMARY_JOB_NAME)
    elif which == "summary":
        targets.append(SUMMARY_JOB_NAME)
    elif which in profile.PLAYBOOKS:
        targets.append(_job_name_for(which))
    else:
        return {"ok": False, "reason": f"unknown target {which!r}"}
    results = []
    for name in targets:
        job = adapter._find_cron_job_by_name(name)
        if not job:
            results.append({"name": name, "ok": False, "reason": "not registered"})
            continue
        success, output, final_response, error = run_job(job)
        results.append({"name": name, "ok": success,
                         "final": final_response, "error": error})
    return {"ok": all(r.get("ok") for r in results), "results": results}


def main() -> int:
    run_now()
    return 0

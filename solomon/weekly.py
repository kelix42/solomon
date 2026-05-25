"""Weekly compression cron.

Loops through the fourteen playbooks plus profile.yaml.summary. For each,
asks the LLM (with solomon-compress.md loaded) to return JSON with a
shorter rewritten version. Playbook diffs are queued for owner review;
the profile.yaml summary is applied immediately because it's a derived
field, not original content.
"""

from __future__ import annotations

import difflib
import fcntl
import json
import re
from pathlib import Path
from typing import Any, Optional

from . import logs, profile

SKILL_PATH = Path(__file__).parent / "skills" / "solomon-compress.md"
TRIVIAL_DIFF_THRESHOLD = 0.10  # if rewritten is within 10% of original size, skip


def _lock_path() -> Path:
    return profile.home() / ".weekly.lock"


def _acquire_lock():
    profile.home().mkdir(parents=True, exist_ok=True)
    f = open(_lock_path(), "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def _release_lock(f) -> None:
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()
        _lock_path().unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _skill_body() -> str:
    text = SKILL_PATH.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---\n", 3)
        if end != -1:
            text = text[end + 5:]
    return text.strip()


def _parse_compression_response(text: str) -> Optional[dict]:
    """Extract JSON {rewritten, summary} from the LLM's response text."""
    # First, try direct parse.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "rewritten" in obj and "summary" in obj:
            return obj
    except json.JSONDecodeError:
        pass
    # Fallback: look for a fenced ```json block.
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict) and "rewritten" in obj and "summary" in obj:
                return obj
        except json.JSONDecodeError:
            return None
    return None


def _compress_one(name: str, current: str, *, adapter: Any) -> Optional[dict]:
    """Ask the LLM to compress one file. Returns parsed JSON or None."""
    system = _skill_body()
    messages = [{"role": "user", "content":
                 f"Playbook: {name}.md\n\nCurrent content:\n\n{current}"}]
    try:
        resp = adapter.llm_call(system=system, messages=messages,
                                  json_mode=True, max_tokens=4096)
    except Exception as e:  # noqa: BLE001
        logs.log_error("error", e, where="weekly._compress_one",
                        context={"file": name})
        return None
    parsed = _parse_compression_response(resp)
    if not parsed:
        logs.log("compression_parse_failed", file=name, level="WARN")
    return parsed


def _diff(old: str, new: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile="old", tofile="new", lineterm="",
        )
    )


def _significant(old: str, new: str) -> bool:
    if not new or new.strip() == old.strip():
        return False
    if len(new) > len(old) * 0.95:
        # Rewrite didn't actually save much. Skip.
        return False
    return True


def run(*, adapter: Optional[Any] = None) -> dict:
    """Compress every playbook + the profile summary. Return summary stats."""
    lock = _acquire_lock()
    if lock is None:
        logs.log("cron_skipped", level="WARN",
                 context={"cron": "weekly", "reason": "lock held"})
        return {"compressed": 0, "skipped": 0, "summary_regenerated": False,
                "lock_skipped": True}

    logs.log("cron_start", context={"cron": "weekly"})
    summary = {"compressed": 0, "skipped": 0, "summary_regenerated": False}
    try:
        if adapter is None:
            logs.log("weekly_no_adapter", level="WARN")
            return summary

        # Playbooks.
        for name in profile.PLAYBOOKS:
            try:
                current = profile.read_playbook(name)
                parsed = _compress_one(name, current, adapter=adapter)
                if parsed is None:
                    summary["skipped"] += 1
                    continue
                if parsed.get("summary", "").strip().lower().startswith("no compression"):
                    summary["skipped"] += 1
                    continue
                if not _significant(current, parsed.get("rewritten", "")):
                    summary["skipped"] += 1
                    continue
                profile.append_review_item({
                    "kind": "compression",
                    "file": name,
                    "section": None,
                    "content": parsed["rewritten"],
                    "reason": parsed.get("summary", "compressed"),
                    "diff": _diff(current, parsed["rewritten"]),
                })
                summary["compressed"] += 1
            except Exception as e:  # noqa: BLE001
                logs.log_error("error", e, where="weekly.run",
                                context={"file": name})
                summary["skipped"] += 1

        # Profile summary: regenerate from current profile + playbooks.
        try:
            import yaml
            data = yaml.safe_load((profile.home() / "profile.yaml").read_text())
            # Build a compact "what's filled" digest for the LLM.
            digest_lines = []
            for sec in ("industry", "belief_system", "why", "principles",
                        "ideal_outcomes", "non_negotiables", "scopes"):
                s = data.get(sec, {}) or {}
                if s.get("filled"):
                    pruned = {k: v for k, v in s.items()
                              if k not in ("filled", "filled_at") and v}
                    digest_lines.append(f"{sec}: {yaml.safe_dump(pruned, sort_keys=False).strip()}")
            digest = "\n\n".join(digest_lines)
            if digest:
                system = (
                    "Write a tight ~500-token summary of this owner's foundation. "
                    "Lead with industry, why, and non-negotiables. Use the owner's "
                    "exact phrases where possible. Plain markdown. No headings."
                )
                messages = [{"role": "user", "content": digest}]
                try:
                    text = adapter.llm_call(system=system, messages=messages,
                                              max_tokens=2048)
                    if text and text.strip():
                        profile.update_profile_summary(text.strip())
                        summary["summary_regenerated"] = True
                        logs.log("summary_regenerated")
                except Exception as e:  # noqa: BLE001
                    logs.log_error("error", e, where="weekly.summary_regenerate")
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where="weekly.summary_regenerate.outer")

        logs.log("cron_end", context={"cron": "weekly", **summary})
    finally:
        _release_lock(lock)
    return summary


def main() -> int:
    run()
    return 0

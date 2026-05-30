"""Atomic, git-tracked file I/O for everything Solomon knows.

All the files under ~/.hermes/solomon/ flow through this module:
- profile.yaml (foundation)
- the fourteen playbook markdown files
- review_queue.jsonl
- pending_actions.jsonl

Every write is atomic (write tempfile + rename), under a per-file POSIX
lock, with a git auto-commit afterward. All string content is passed
through the PII redaction pass before any write hits disk.

The LLM only ever sees text through `tools.py`, which calls into this
module. Nothing else writes to these files at runtime.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from . import logs

# ---------------------------------------------------------------------------
# Filenames and structure
# ---------------------------------------------------------------------------

PLAYBOOKS = (
    "vocabulary",
    "customers",
    "vendors",
    "operations",
    "sales",
    "marketing",
    "finance",
    "people",
    "product",
    "support",
    "legal",
    "technology",
    "strategy",
    "procurement",
)

PROFILE_SECTIONS = (
    "meta",
    "industry",
    "belief_system",
    "why",
    "principles",
    "ideal_outcomes",
    "non_negotiables",
    "scopes",
    "summary",
)

# Maps session number to the profile.yaml section it fills.
SESSION_SECTION = {
    0: "industry",
    1: "belief_system",
    2: "why",
    3: "principles",
    4: "ideal_outcomes",
    5: "non_negotiables",
    6: "scopes",
}

SESSION_NAMES = {
    0: "Industry & sector",
    1: "Belief system",
    2: "Why",
    3: "Principles",
    4: "Ideal outcomes",
    5: "Non-negotiables",
    6: "Scopes",
}

# Required fields per session (used by tools.mark_session_complete validation).
SESSION_REQUIRED_FIELDS = {
    0: ("business_category", "primary_product_or_service", "customer_orientation",
        "geographic_scope", "revenue_model", "growth_stage", "concentration_risk"),
    1: ("core_beliefs", "what_they_reject"),
    2: ("short", "long", "not_for"),
    3: ("decision_principles", "trade_off_principles"),
    4: ("one_year", "five_year", "failure_picture"),
    5: ("rules",),
    6: ("list", "preferred_channel"),
}


def home() -> Path:
    return logs.home()


# ---------------------------------------------------------------------------
# Templates (used by init_solomon_home)
# ---------------------------------------------------------------------------

EMPTY_PROFILE: dict[str, Any] = {
    "meta": {
        "schema_version": 1,
        "last_updated": None,
        "owner_name": "",
        "business_name": "",
        "preferred_channel": "",
        "nudge_cadence": {
            "high": "1h then 2h",
            "medium": "4h then 6h",
            "low": "12h then 24h",
            "max_nudges": 3,
        },
    },
    "industry": {
        "filled": False,
        "filled_at": None,
        "business_category": "",
        "primary_product_or_service": "",
        "customer_orientation": "",
        "geographic_scope": "",
        "revenue_model": "",
        "growth_stage": "",
        "concentration_risk": "",
    },
    "belief_system": {
        "filled": False,
        "filled_at": None,
        "core_beliefs": [],
        "what_they_reject": [],
    },
    "why": {"filled": False, "filled_at": None, "short": "", "long": "", "not_for": []},
    "principles": {
        "filled": False,
        "filled_at": None,
        "decision_principles": [],
        "trade_off_principles": [],
    },
    "ideal_outcomes": {
        "filled": False,
        "filled_at": None,
        "one_year": "",
        "five_year": "",
        "failure_picture": "",
    },
    "non_negotiables": {"filled": False, "filled_at": None, "rules": []},
    "scopes": {"filled": False, "filled_at": None, "list": []},
    "summary": {"text": "", "generated_at": None},
}


def _playbook_template(name: str) -> str:
    """The empty template every playbook starts as."""
    titles = {
        "vocabulary": ("Vocabulary",
                       "The owner's exact phrases. Used by Solomon to speak in the owner's voice."),
        "customers": ("Customers", "Who buys, what they want, how they behave."),
        "vendors": ("Vendors", "Who you work with and what they're like."),
        "operations": ("Operations", "Making the product or delivering the service, plus day-to-day running."),
        "sales": ("Sales", "Getting customers to actually buy."),
        "marketing": ("Marketing", "Awareness and demand creation."),
        "finance": ("Finance", "Money, cash flow, taxes, budgeting, reporting. Pricing rules live here."),
        "people": ("People", "Hiring, paying, managing, and developing employees."),
        "product": ("Product", "Designing and improving what the business sells."),
        "support": ("Support", "Helping customers after they buy."),
        "legal": ("Legal", "Contracts, regulations, risk, and liability."),
        "technology": ("Technology", "The systems, software, and infrastructure the business runs on."),
        "strategy": ("Strategy", "Direction-setting, decision-making, and governance."),
        "procurement": ("Procurement", "Sourcing inputs and managing suppliers and logistics."),
    }
    title, purpose = titles[name]
    if name == "vocabulary":
        return (
            f"# {title}\n\n{purpose}\n\nLast updated: never\n\n"
            "## Phrases the owner uses\n\n"
            "<!-- One phrase per bullet, with a verbatim example sentence in quotes. -->\n\n"
            "## Phrases the owner avoids\n\n"
            "<!-- Things they would never say. -->\n\n"
            "## Tone notes\n\n"
            "<!-- How they speak — terse, story-driven, etc. -->\n\n"
            "## See also\n\n"
            "<!-- Cross-references to other files will appear here as the playbooks grow. -->\n"
        )
    return (
        f"# {title}\n\n{purpose}\n\nLast updated: never\n\n"
        "<!-- This file is empty. Solomon will add sections here as it captures rules from your "
        "conversations and documents. -->\n\n"
        "## See also\n\n"
        "<!-- Cross-references to other files will appear here as the playbooks grow. -->\n"
    )


GITIGNORE = (
    "inbox/\n"
    "archive/\n"
    "logs/\n"
    ".solomon_off\n"
    ".daily.lock\n"
    ".weekly.lock\n"
    ".checkin.lock\n"
    "pending_messages.jsonl\n"
)


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------

# Patterns are intentionally conservative. We replace, log, and move on.
# Order matters: more specific patterns first so they win over generic ones.
_REDACTION_PATTERNS = [
    # US SSN: 123-45-6789
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]", "ssn"),
    # Canadian SIN: 123-456-789
    (re.compile(r"\b\d{3}-\d{3}-\d{3}\b"), "[SIN]", "sin"),
    # Credit card-like 13–19 digit groups (with optional spaces/dashes).
    # We then verify Luhn before redacting to cut false positives.
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "__CARD_CANDIDATE__", "card"),
    # Email addresses.
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]", "email"),
    # US/Canadian phone numbers in common formats.
    (re.compile(r"(?:\+?1[ -.]?)?(?:\(\d{3}\)\s*|\d{3}[ -.])\d{3}[ -.]\d{4}"), "[PHONE]", "phone"),
    # Passport-like: 9 chars, mix of letters and digits, no spaces, with at least one of each.
    (re.compile(r"\b(?=\w*[A-Z])(?=\w*\d)[A-Z0-9]{6,9}\b"), "[PASSPORT]", "passport"),
]


def _luhn_ok(s: str) -> bool:
    digits = [int(c) for c in s if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact(text: str) -> str:
    """Return text with sensitive patterns replaced by placeholders.

    Logs a `redaction_applied` event per match so the audit trail shows
    what was redacted from which file (the file context is set by the
    caller via a logging adapter elsewhere; here we just record the kind).
    """
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pattern, replacement, kind in _REDACTION_PATTERNS:
        def _sub(match: re.Match) -> str:
            matched = match.group(0)
            if kind == "card":
                if not _luhn_ok(matched):
                    return matched  # not a real card; leave alone
                logs.log("redaction_applied", kind=kind, level="DEBUG")
                return "[CARD]"
            logs.log("redaction_applied", kind=kind, level="DEBUG")
            return replacement
        out = pattern.sub(_sub, out)
    # Clean up any leftover "__CARD_CANDIDATE__" markers we never replaced.
    out = out.replace("__CARD_CANDIDATE__", "")
    return out


def _redact_any(value: Any) -> Any:
    """Recursively apply redaction to strings inside dicts/lists."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_any(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_any(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Locking and atomic writes
# ---------------------------------------------------------------------------


@contextmanager
def _file_lock(target: Path):
    """Hold an exclusive POSIX lock on a sentinel next to `target`."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_suffix(target.suffix + ".lock")
    f = open(lock_path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _atomic_write(target: Path, content: str) -> None:
    """Write to a temp file in the same directory, then rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, target)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _atomic_append(target: Path, line: str) -> None:
    """Append one line atomically by reading existing content and rewriting."""
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    _atomic_write(target, existing + line.rstrip("\n") + "\n")


# ---------------------------------------------------------------------------
# Git auto-commit
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or home()),
        capture_output=True,
        text=True,
        check=False,
    )


def _ensure_git_repo() -> None:
    h = home()
    if (h / ".git").exists():
        return
    _git("init", "-q")
    # Configure a no-op identity so commits work even on a fresh machine.
    _git("config", "user.email", "solomon@local")
    _git("config", "user.name", "Solomon")
    # Touch the gitignore so it's tracked.
    (h / ".gitignore").write_text(GITIGNORE)
    _git("add", ".gitignore")
    _git("commit", "-q", "-m", "Solomon initialized")


def _commit(message: str, paths: Iterable[str]) -> None:
    try:
        _ensure_git_repo()
        for p in paths:
            _git("add", "--", p)
        result = _git("commit", "-q", "-m", message)
        if result.returncode == 0:
            logs.log("git_commit", level="DEBUG", summary=message)
    except Exception as e:  # noqa: BLE001
        logs.log_error("error", e, where="profile._commit")


# ---------------------------------------------------------------------------
# Scaffold
# ---------------------------------------------------------------------------


def init_solomon_home() -> Path:
    """Idempotent. Create the home folder, all empty templates, and git."""
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    (h / "inbox").mkdir(exist_ok=True)
    (h / "archive").mkdir(exist_ok=True)
    (h / "logs").mkdir(exist_ok=True)
    # Gitignore (only if missing — don't clobber owner edits).
    gi = h / ".gitignore"
    if not gi.exists():
        gi.write_text(GITIGNORE)
    # Profile.
    pf = h / "profile.yaml"
    if not pf.exists():
        _atomic_write(pf, yaml.safe_dump(EMPTY_PROFILE, sort_keys=False))
    # Playbooks.
    for name in PLAYBOOKS:
        p = h / f"{name}.md"
        if not p.exists():
            _atomic_write(p, _playbook_template(name))
    # Queues (empty files so jsonl appends work without special-casing).
    rq = h / "review_queue.jsonl"
    if not rq.exists():
        rq.touch()
    pa = h / "pending_actions.jsonl"
    if not pa.exists():
        pa.touch()
    # First git commit.
    _ensure_git_repo()
    _git("add", ".")
    _git("commit", "-q", "-m", "Solomon home scaffolded")
    return h


# ---------------------------------------------------------------------------
# Profile reads/writes
# ---------------------------------------------------------------------------


def _load_profile() -> dict[str, Any]:
    path = home() / "profile.yaml"
    if not path.exists():
        init_solomon_home()
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        logs.log_error("error", e, where="profile._load_profile")
        return {}


def _dump_profile(data: dict[str, Any]) -> None:
    path = home() / "profile.yaml"
    with _file_lock(path):
        _atomic_write(path, yaml.safe_dump(data, sort_keys=False))


def read_profile_section(section: str) -> str:
    """Return one profile.yaml section as a YAML string (or sentinel if empty)."""
    if section not in PROFILE_SECTIONS:
        raise ValueError(f"unknown profile section {section!r}; valid: {PROFILE_SECTIONS}")
    data = _load_profile()
    value = data.get(section)
    if value is None or (isinstance(value, dict) and not value):
        return "(section not yet filled)"
    if isinstance(value, dict) and value.get("filled") is False:
        return "(section not yet filled)"
    return yaml.safe_dump({section: value}, sort_keys=False).strip()


def onboarding_status() -> dict:
    """How far onboarding has progressed, by filled session section.

    Returns ``{total, filled, completed, remaining}`` where completed/remaining
    are the human-readable session names. Used so Solomon can report accurate
    partial-completion state instead of treating a half-built profile as empty.
    """
    data = _load_profile()
    completed: list[str] = []
    remaining: list[str] = []
    for n in sorted(SESSION_SECTION):
        target = completed if data.get(SESSION_SECTION[n], {}).get("filled") else remaining
        target.append(SESSION_NAMES[n])
    return {
        "total": len(SESSION_SECTION),
        "filled": len(completed),
        "completed": completed,
        "remaining": remaining,
    }


def write_session_summary(session_n: int, summary: dict) -> None:
    """Used by mark_session_complete. Validates and writes the section."""
    if session_n not in SESSION_SECTION:
        raise ValueError(f"unknown session number {session_n}; valid: 0-6")
    section = SESSION_SECTION[session_n]
    required = SESSION_REQUIRED_FIELDS[session_n]
    missing = [k for k in required if k not in summary]
    if missing:
        raise ValueError(f"session {session_n} summary missing fields: {missing}")
    # Redact and merge.
    redacted = _redact_any(summary)
    data = _load_profile()
    section_data = data.get(section, {}) or {}
    section_data.update(redacted)
    section_data["filled"] = True
    now = datetime.now(timezone.utc).isoformat()
    section_data["filled_at"] = now
    data[section] = section_data
    # If session 6 carries preferred_channel/nudge_cadence_override, also update meta.
    if session_n == 6:
        meta = data.setdefault("meta", {})
        if "preferred_channel" in redacted:
            meta["preferred_channel"] = redacted["preferred_channel"]
        if "nudge_cadence_override" in redacted and redacted["nudge_cadence_override"]:
            meta["nudge_cadence"] = redacted["nudge_cadence_override"]
    data.setdefault("meta", {})["last_updated"] = now
    _dump_profile(data)
    _commit(f"completed session {session_n} — {SESSION_NAMES[session_n]}", ["profile.yaml"])


def update_profile_summary(text: str) -> None:
    """Used by the weekly compression cron to update the always-loaded summary."""
    data = _load_profile()
    data.setdefault("summary", {})
    data["summary"]["text"] = redact(text)
    data["summary"]["generated_at"] = datetime.now(timezone.utc).isoformat()
    _dump_profile(data)
    _commit("regenerated profile summary", ["profile.yaml"])


# ---------------------------------------------------------------------------
# Playbook reads/writes
# ---------------------------------------------------------------------------


def read_playbook(name: str) -> str:
    if name not in PLAYBOOKS:
        raise ValueError(f"unknown playbook {name!r}; valid: {PLAYBOOKS}")
    path = home() / f"{name}.md"
    if not path.exists():
        init_solomon_home()
    return path.read_text(encoding="utf-8")


def write_playbook(name: str, content: str) -> None:
    if name not in PLAYBOOKS:
        raise ValueError(f"unknown playbook {name!r}; valid: {PLAYBOOKS}")
    path = home() / f"{name}.md"
    redacted = redact(content)
    with _file_lock(path):
        _atomic_write(path, redacted)
    _commit(f"updated playbook: {name}", [f"{name}.md"])


def insert_into_playbook(name: str, section: str, content: str) -> None:
    """Append `content` under heading `section` in playbook `name`.

    Creates the heading if it doesn't exist (placed right before "## See also"
    if that section exists, otherwise at the end). Used by apply_queue_decision.
    """
    if name not in PLAYBOOKS:
        raise ValueError(f"unknown playbook {name!r}")
    path = home() / f"{name}.md"
    if not path.exists():
        init_solomon_home()
    original = path.read_text(encoding="utf-8")
    redacted_content = redact(content)

    heading = f"## {section}"
    if heading in original:
        # Insert after the heading, before the next "## " heading.
        lines = original.splitlines(keepends=True)
        out: list[str] = []
        inserted = False
        for i, line in enumerate(lines):
            out.append(line)
            if inserted:
                continue
            if line.rstrip() == heading:
                # Find where this section ends (next H2 or end of file).
                j = i + 1
                while j < len(lines) and not lines[j].startswith("## "):
                    j += 1
                # Insert before line j.
                out.extend(lines[i + 1:j])
                # Now append our new content with a blank line before/after.
                if out and not out[-1].endswith("\n\n"):
                    if not out[-1].endswith("\n"):
                        out.append("\n")
                    out.append("\n")
                out.append(redacted_content.rstrip() + "\n\n")
                out.extend(lines[j:])
                inserted = True
                break
        new = "".join(out)
    else:
        # New heading. Place before "## See also" if present.
        see_also = "## See also"
        new_section = f"{heading}\n\n{redacted_content.rstrip()}\n\n"
        if see_also in original:
            new = original.replace(see_also, new_section + see_also)
        else:
            new = original.rstrip() + "\n\n" + new_section

    # Update the "Last updated" line.
    now = datetime.now(timezone.utc).date().isoformat()
    new = re.sub(r"Last updated: .+", f"Last updated: {now}", new, count=1)

    with _file_lock(path):
        _atomic_write(path, new)
    _commit(f"insert into {name}.md → {section}", [f"{name}.md"])


# ---------------------------------------------------------------------------
# Queue I/O
# ---------------------------------------------------------------------------


def _queue_path(kind: str) -> Path:
    if kind == "review":
        return home() / "review_queue.jsonl"
    if kind == "actions":
        return home() / "pending_actions.jsonl"
    raise ValueError(f"unknown queue {kind!r}")


def _next_sequence(queue: str, prefix: str) -> str:
    """Return the next id like 'q_YYYY-MM-DD_001' for today's date."""
    today = datetime.now(timezone.utc).date().isoformat()
    path = _queue_path(queue)
    count = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            iid = entry.get("id", "")
            if iid.startswith(f"{prefix}_{today}_"):
                count += 1
    return f"{prefix}_{today}_{count + 1:03d}"


def append_review_item(item: dict) -> str:
    item.setdefault("id", _next_sequence("review", "q"))
    item.setdefault("ts", datetime.now(timezone.utc).isoformat())
    item.setdefault("status", "pending")
    redacted = _redact_any(item)
    path = _queue_path("review")
    with _file_lock(path):
        _atomic_append(path, json.dumps(redacted, ensure_ascii=False))
    _commit(f"queue: {redacted.get('kind', 'item')} {redacted['id']}", ["review_queue.jsonl"])
    return redacted["id"]


def append_action_item(item: dict) -> str:
    item.setdefault("id", _next_sequence("actions", "a"))
    item.setdefault("ts", datetime.now(timezone.utc).isoformat())
    item.setdefault("status", "pending")
    item.setdefault("nudge_count", 0)
    redacted = _redact_any(item)
    path = _queue_path("actions")
    with _file_lock(path):
        _atomic_append(path, json.dumps(redacted, ensure_ascii=False))
    _commit(
        f"action queued ({redacted['id']}): {redacted.get('source_kind', '?')} — {redacted.get('action_kind', '?')}",
        ["pending_actions.jsonl"],
    )
    return redacted["id"]


def read_queue(queue: str = "review", status: str = "pending", limit: int = 20) -> list[dict]:
    """Return items from the named queue filtered by status."""
    path = _queue_path(queue)
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if status == "all" or entry.get("status") == status:
            out.append(entry)
    return out[:limit]


def find_action_by_source(source_kind: str, source_id: str) -> Optional[dict]:
    """Locate an existing pending_actions item by its source identifier."""
    for entry in read_queue("actions", status="all", limit=10_000):
        if entry.get("source_kind") == source_kind and entry.get("source_id") == source_id:
            return entry
    return None


def update_queue_item(queue: str, item_id: str, updates: dict) -> bool:
    """Rewrite the JSONL with one item's fields merged. Returns True on success."""
    path = _queue_path(queue)
    if not path.exists():
        return False
    redacted_updates = _redact_any(updates)
    found = False
    new_lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            new_lines.append(line)
            continue
        if entry.get("id") == item_id:
            entry.update(redacted_updates)
            found = True
        new_lines.append(json.dumps(entry, ensure_ascii=False))
    if not found:
        return False
    with _file_lock(path):
        _atomic_write(path, "\n".join(new_lines) + "\n")
    _commit(f"queue update: {item_id} → {redacted_updates.get('status', 'updated')}",
            [path.name])
    return True


def find_queue_item(queue: str, item_id: str) -> Optional[dict]:
    path = _queue_path(queue)
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("id") == item_id:
            return entry
    return None


# ---------------------------------------------------------------------------
# Compression archival
# ---------------------------------------------------------------------------


def archive_playbook_version(name: str) -> Optional[Path]:
    """Copy the current playbook into archive/compressed/<date>/ before replacing.

    Returns the archive path written, or None if the source didn't exist.
    Used by apply_queue_decision when applying an approved compression.
    """
    src = home() / f"{name}.md"
    if not src.exists():
        return None
    today = datetime.now(timezone.utc).date().isoformat()
    dst_dir = home() / "archive" / "compressed" / today
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{name}.md"
    # If a previous archive of this name exists today, append a suffix.
    if dst.exists():
        i = 1
        while (dst_dir / f"{name}.{i}.md").exists():
            i += 1
        dst = dst_dir / f"{name}.{i}.md"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst

"""Onboarding session runner — 5-stage flow over the interview engine.

This is the CLI entry point (``solomon onboard session_N``). It drives one
domain interview from "session open" to "foundation YAML written" using
the modules under ``solomon.onboarding.interview``.

The five stages (per docs/REPORT-INTERVIEW.md §4.4):

  A. Setup — open or resume the ``sessions`` row, seed coverage rows.
  B. Discovery — engine.select_next_probe → owner answer → redact +
     extract + vocabulary + contradiction. Loop until coverage says done.
  C. Required-fields pass — ask each unfilled required_field from the
     probe library directly. Hard 2-turn cap per field. Captured rows
     get ``field:<id>`` injected into ``keywords``.
  D. Closing checkpoint — read-back of what was captured. Owner picks
     one of {confirm, correct, add, keep_talking, abandon}. Loop until
     ``confirm`` or ``abandon``.
  E. Close — render ``foundation/NN-<domain>.yaml`` and mark the session
     ``complete``.

No external orchestrator. Pure Python + the storage pool + the engine
modules. Designed to be safe to Ctrl-C: the ``sessions`` row stays
``open`` until Stage E succeeds, so re-running ``solomon onboard
session_N`` resumes from wherever you stopped.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from ..reasoning.llm import get_client
from ..storage.pool import cursor, execute, get_conn, init_storage, parse_json
from .interview import ELIZA_SYSTEM_PROMPT
from .interview import contradiction as contradiction_mod
from .interview import coverage as coverage_mod
from .interview import engine as engine_mod
from .interview import extraction as extraction_mod
from .interview import redact as redact_mod
from .interview import vocabulary as vocab_mod

logger = logging.getLogger("solomon.onboarding")

FOUNDATION_DIR = Path(os.path.expanduser("~/.hermes/solomon/foundation"))

# session_N → (domain, ordinal). Drive's eight onboarding sessions, minus
# the dedicated mentoring-only domains. session_0 is industry — the floor
# every other session builds on.
SESSION_DOMAINS: Dict[str, Tuple[str, int]] = {
    "session_0": ("industry", 0),
    "session_1": ("belief_system", 1),
    "session_2": ("why", 2),
    "session_3": ("principles", 3),
    "session_4": ("ideal_outcomes", 4),
    "session_5": ("non_negotiables", 5),
    "session_6": ("scopes", 6),
}

# Max turns the required-fields pass will spend on any single field
# before moving on. Per docs/REPORT-INTERVIEW.md §1.1.3 (required_fields).
_REQUIRED_FIELD_TURN_CAP = 2

# Max turns Stage D will loop on owner intent before forcing close. Safety
# valve; in normal flow the owner picks "confirm" or "abandon" quickly.
_CHECKPOINT_TURN_CAP = 6


# ---------------------------------------------------------------------------
# Tenant / session bootstrap
# ---------------------------------------------------------------------------

def _ensure_tenant() -> str:
    """Return the active tenant_id, creating the row if needed.

    Uses the new storage pool API directly so we don't depend on the older
    decisions.get_or_create_tenant_id which still uses %s placeholders.
    """
    tenant_id = os.getenv("SOLOMON_TENANT_ID", "default")
    business_name = os.getenv("SOLOMON_BUSINESS_NAME", "My Business")
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "INSERT INTO tenants (tenant_id, business_name) "
                    "VALUES (?, ?) ON CONFLICT (tenant_id) DO NOTHING",
                    (tenant_id, business_name),
                )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("ensure tenant failed: %s", e)
    return tenant_id


def _session_id_for(domain: str, ordinal: int) -> str:
    """Canonical session id: onboarding-NN-<domain>-YYYYMMDD.

    Stable per calendar day so that a same-day resume hits the same row;
    a fresh day starts a new session if the prior one is already complete.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"onboarding-{ordinal:02d}-{domain}-{stamp}"


def _open_or_resume_session(
    tenant_id: str,
    domain: str,
    ordinal: int,
    library_version: str,
) -> Tuple[str, bool]:
    """Open a new session row, or return an existing open one for this domain.

    Returns (session_id, resumed). resumed=True iff we matched an already-
    open row for the same (tenant, domain) — even if today's stamp differs.
    """
    # Resume: any open row for this tenant + domain, prefer the newest.
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "SELECT session_id FROM sessions "
                    "WHERE tenant_id=? AND domain=? AND status='open' "
                    "ORDER BY started_at DESC LIMIT 1",
                    (tenant_id, domain),
                )
                row = cur.fetchone()
        if row:
            sid = row[0] if not hasattr(row, "keys") else row["session_id"]
            return str(sid), True
    except Exception as e:  # noqa: BLE001
        logger.warning("session resume lookup failed: %s", e)

    sid = _session_id_for(domain, ordinal)
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "INSERT INTO sessions (session_id, tenant_id, domain, mode, "
                    "status, library_version) VALUES (?, ?, ?, 'onboarding', "
                    "'open', ?)",
                    (sid, tenant_id, domain, library_version),
                )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        # Likely a UNIQUE conflict because today's stamp already exists.
        logger.warning("session insert failed (likely exists): %s", e)
    return sid, False


def _seed_coverage(tenant_id: str, session_id: str, domain: str, library: Dict[str, Any]) -> None:
    """Insert one coverage row per keyword cluster in the library, so the
    coverage tracker has something to reason about from turn 1.

    Idempotent via the UNIQUE (tenant_id, session_id, domain, sub_topic).
    """
    keywords = (library.get("keywords") or {})
    sub_topics = list(keywords.keys()) or ["_open"]
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                for sub in sub_topics:
                    execute(
                        cur,
                        "INSERT INTO coverage (tenant_id, session_id, domain, "
                        "sub_topic) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT (tenant_id, session_id, domain, sub_topic) "
                        "DO NOTHING",
                        (tenant_id, session_id, domain, str(sub)),
                    )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("coverage seed failed: %s", e)


# ---------------------------------------------------------------------------
# Turn helpers
# ---------------------------------------------------------------------------

def _process_owner_turn(
    session_id: str,
    domain: str,
    turn_number: int,
    owner_text: str,
    tenant_id: str,
    extra_keyword_tag: Optional[str] = None,
) -> List[str]:
    """Run the full post-turn pipeline: redact → extract → vocab → contradiction.

    Returns the list of new captured_items ids. ``extra_keyword_tag`` is
    appended to each new row's ``keywords`` JSON list — used in Stage C
    to mark which row satisfies which required_field (``field:<id>``).
    """
    if not owner_text or not owner_text.strip():
        return []

    redacted = redact_mod.redact(owner_text)

    # Extraction is the only side-effecting call into captured_items.
    new_ids = extraction_mod.extract(session_id, turn_number, redacted, domain)

    # Tag with field:<id> if Stage C asked us to.
    if extra_keyword_tag and new_ids:
        _append_keyword_tag(new_ids, extra_keyword_tag)

    # Vocabulary capture runs regardless of whether extraction landed rows.
    try:
        vocab_mod.capture(redacted, tenant_id=tenant_id,
                          source_item_id=new_ids[0] if new_ids else None)
    except Exception as e:  # noqa: BLE001
        logger.warning("vocabulary capture failed: %s", e)

    # Contradiction check per new row.
    for item_id in new_ids:
        try:
            contradiction_mod.check(item_id, tenant_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("contradiction check failed for %s: %s", item_id, e)

    # If nothing landed, bump turns_since_last_capture across the session.
    if not new_ids:
        coverage_mod.refresh(session_id, domain, captured_count_delta=0)

    return new_ids


def _append_keyword_tag(item_ids: List[str], tag: str) -> None:
    """Append `tag` to the keywords JSON list of each captured_items row."""
    if not item_ids or not tag:
        return
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                for iid in item_ids:
                    execute(cur, "SELECT keywords FROM captured_items WHERE id=?", (iid,))
                    r = cur.fetchone()
                    if not r:
                        continue
                    raw = r[0] if not hasattr(r, "keys") else r["keywords"]
                    lst = parse_json(raw) or []
                    if not isinstance(lst, list):
                        lst = []
                    if tag not in lst:
                        lst.append(tag)
                    execute(
                        cur,
                        "UPDATE captured_items SET keywords=? WHERE id=?",
                        (json.dumps(lst), iid),
                    )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("keyword tag append failed: %s", e)


def _prompt_owner(question: str, input_fn: Callable[[str], str], turn_number: int) -> str:
    """Render one Q&A pair and return the owner's text."""
    print(f"\nSolomon › {question}")
    try:
        return input_fn("You    › ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n(session paused — re-run `solomon onboard <session>` to resume)")
        raise


# ---------------------------------------------------------------------------
# Stage D — closing checkpoint
# ---------------------------------------------------------------------------

_INTENT_SYSTEM = (
    "Classify the owner's reply into exactly one intent. Return JSON: "
    "{\"intent\": one of [confirm, correct, add, keep_talking, abandon]}.\n"
    "- confirm: the owner agrees the summary is right.\n"
    "- correct: the owner is fixing or contradicting something in the summary.\n"
    "- add: the owner is supplying a new fact not yet in the summary.\n"
    "- keep_talking: the owner wants more interview time before closing.\n"
    "- abandon: the owner wants to stop and not save."
)


def _classify_intent(owner_text: str) -> str:
    """LLM intent classifier with a deterministic fallback."""
    if not owner_text or not owner_text.strip():
        return "keep_talking"
    text = owner_text.strip().lower()
    # Deterministic short-circuit. The owner often types one of these.
    if text in {"y", "yes", "yeah", "yep", "confirm", "good", "ok", "looks good", "lgtm"}:
        return "confirm"
    if text in {"abandon", "abort", "quit", "stop", "exit"}:
        return "abandon"
    if text.startswith(("keep going", "more", "continue")):
        return "keep_talking"

    client = get_client()
    if not client.configured:
        # Without an LLM, treat anything not matching the deterministic list
        # as "add" (safest — we'll capture the row).
        return "add"
    try:
        resp = client.call(
            tier="fast",
            system=_INTENT_SYSTEM,
            user=owner_text,
            json_mode=True,
            max_tokens=64,
            temperature=0.0,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("intent classifier call failed: %s", e)
        return "add"
    parsed = client.parse_json(resp.text) or {}
    intent = str(parsed.get("intent") or "").strip().lower()
    if intent in {"confirm", "correct", "add", "keep_talking", "abandon"}:
        return intent
    return "add"


def _checkpoint_summary(tenant_id: str, session_id: str, domain: str) -> str:
    """Plain-text read-back of what we've captured so far."""
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "SELECT statement, verbatim_phrase, confidence FROM captured_items "
                    "WHERE tenant_id=? AND session_id=? AND domain=? "
                    "ORDER BY created_at ASC",
                    (tenant_id, session_id, domain),
                )
                rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning("checkpoint summary query failed: %s", e)
        return "(no captures to read back)"
    if not rows:
        return "(no captures to read back)"
    lines = []
    for r in rows:
        if hasattr(r, "keys"):
            stmt, verb, conf = r["statement"], r["verbatim_phrase"], r["confidence"]
        else:
            stmt, verb, conf = r[0], r[1], r[2]
        lines.append(f"  - [{conf}] {stmt}  (\"{verb}\")")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage E — render foundation YAML
# ---------------------------------------------------------------------------

def _render_foundation(
    tenant_id: str,
    session_id: str,
    domain: str,
    ordinal: int,
    library: Dict[str, Any],
) -> Path:
    """Write ~/.hermes/solomon/foundation/NN-<domain>.yaml from captured_items.

    Three SQL result sets per docs/REPORT-INTERVIEW.md §1.1.2 F3:
      - required_fields  (latest captured row per field tag)
      - discovery        (everything else captured this session)
      - vocabulary       (top-30 phrases by frequency for this tenant)
    """
    FOUNDATION_DIR.mkdir(parents=True, exist_ok=True)
    out_path = FOUNDATION_DIR / f"{ordinal:02d}-{domain}.yaml"

    required = (library.get("required_fields") or [])
    rf_ids = [f.get("id") for f in required if isinstance(f, dict) and f.get("id")]

    required_section: Dict[str, Any] = {}
    discovery_section: List[Dict[str, Any]] = []
    vocab_section: List[Dict[str, Any]] = []

    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                # Required-fields: latest row tagged field:<id>
                for fid in rf_ids:
                    execute(
                        cur,
                        "SELECT statement, verbatim_phrase, example, confidence "
                        "FROM captured_items "
                        "WHERE tenant_id=? AND session_id=? AND keywords LIKE ? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (tenant_id, session_id, f'%"field:{fid}"%'),
                    )
                    r = cur.fetchone()
                    if r:
                        required_section[fid] = {
                            "statement": r[0] if not hasattr(r, "keys") else r["statement"],
                            "verbatim_phrase": r[1] if not hasattr(r, "keys") else r["verbatim_phrase"],
                            "example": r[2] if not hasattr(r, "keys") else r["example"],
                            "confidence": r[3] if not hasattr(r, "keys") else r["confidence"],
                        }
                    else:
                        required_section[fid] = None

                # Discovery: every captured row this session that isn't
                # a required-field tag.
                execute(
                    cur,
                    "SELECT id, type, statement, verbatim_phrase, example, "
                    "confidence, keywords FROM captured_items "
                    "WHERE tenant_id=? AND session_id=? AND domain=? "
                    "ORDER BY created_at ASC",
                    (tenant_id, session_id, domain),
                )
                for r in cur.fetchall():
                    if hasattr(r, "keys"):
                        kw = parse_json(r["keywords"]) or []
                    else:
                        kw = parse_json(r[6]) or []
                    if isinstance(kw, list) and any(
                        isinstance(k, str) and k.startswith("field:") for k in kw
                    ):
                        # Already reported in required_section.
                        continue
                    discovery_section.append({
                        "type": r[1] if not hasattr(r, "keys") else r["type"],
                        "statement": r[2] if not hasattr(r, "keys") else r["statement"],
                        "verbatim_phrase": r[3] if not hasattr(r, "keys") else r["verbatim_phrase"],
                        "example": r[4] if not hasattr(r, "keys") else r["example"],
                        "confidence": r[5] if not hasattr(r, "keys") else r["confidence"],
                        "keywords": kw if isinstance(kw, list) else [],
                    })

                # Vocabulary: top 30 by frequency for this tenant
                execute(
                    cur,
                    "SELECT phrase, normalised, kind, frequency FROM vocabulary "
                    "WHERE tenant_id=? ORDER BY frequency DESC, last_seen DESC LIMIT 30",
                    (tenant_id,),
                )
                for r in cur.fetchall():
                    vocab_section.append({
                        "phrase": r[0] if not hasattr(r, "keys") else r["phrase"],
                        "normalised": r[1] if not hasattr(r, "keys") else r["normalised"],
                        "kind": r[2] if not hasattr(r, "keys") else r["kind"],
                        "frequency": r[3] if not hasattr(r, "keys") else r["frequency"],
                    })
    except Exception as e:  # noqa: BLE001
        logger.error("foundation render query failed: %s", e)

    doc = {
        "domain": domain,
        "session_id": session_id,
        "library_version": library.get("version"),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "required_fields": required_section,
        "discovery": discovery_section,
        "vocabulary_top": vocab_section,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, sort_keys=False)
    return out_path


def _mark_session(session_id: str, status: str) -> None:
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                if status == "complete":
                    execute(
                        cur,
                        "UPDATE sessions SET status='complete', "
                        "completed_at=datetime('now') WHERE session_id=?",
                        (session_id,),
                    )
                else:
                    execute(
                        cur,
                        "UPDATE sessions SET status=? WHERE session_id=?",
                        (status, session_id),
                    )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("session status update failed: %s", e)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_session(
    session_key: str,
    *,
    input_fn: Optional[Callable[[str], str]] = None,
    print_fn: Optional[Callable[[str], None]] = None,
    max_discovery_turns: int = 40,
) -> Dict[str, Any]:
    """Drive one onboarding session from open to YAML.

    ``input_fn`` and ``print_fn`` are injectable so tests can drive the
    session deterministically without monkeypatching builtins.
    """
    if session_key not in SESSION_DOMAINS:
        raise ValueError(
            f"Unknown session: {session_key}. Known: {sorted(SESSION_DOMAINS)}"
        )
    domain, ordinal = SESSION_DOMAINS[session_key]

    in_fn = input_fn or (lambda prompt: input(prompt))
    # print_fn is reserved for future test injection; currently the runner
    # uses stdout directly via builtins.print. We accept the kwarg so tests
    # can pass it without breaking, even though we don't redirect yet.
    _ = print_fn

    # ----- Stage A. Setup ---------------------------------------------------
    init_storage()
    tenant_id = _ensure_tenant()
    library = engine_mod.load_library(domain)
    library_version = str(library.get("version") or "0.0.0")
    session_id, resumed = _open_or_resume_session(tenant_id, domain, ordinal, library_version)
    _seed_coverage(tenant_id, session_id, domain, library)

    print(f"\n=== Solomon onboarding · {session_key} · domain={domain} ===")
    if resumed:
        print(f"(resuming open session {session_id})")
    print(
        "Talk freely. Solomon will reflect your words back as it goes. "
        "Ctrl-C pauses and saves; re-run the command to resume.\n"
    )

    # ----- Stage B. Discovery loop -----------------------------------------
    last_answer = ""
    turn = 0
    discovery_captures: List[str] = []
    try:
        while turn < max_discovery_turns:
            probe = engine_mod.select_next_probe(session_id, domain, last_answer)
            owner_text = _prompt_owner(probe, in_fn, turn)
            turn += 1
            if owner_text.lower() in {"/done", "/stop", "/end", "/abort"}:
                break
            new_ids = _process_owner_turn(
                session_id, domain, turn, owner_text, tenant_id
            )
            discovery_captures.extend(new_ids)
            last_answer = owner_text
            if coverage_mod.is_session_complete(session_id, domain):
                break
    except (EOFError, KeyboardInterrupt):
        return {
            "session_id": session_id,
            "domain": domain,
            "status": "paused",
            "captures": len(discovery_captures),
        }

    # ----- Stage C. Required-fields pass -----------------------------------
    required: List[Dict[str, Any]] = list(library.get("required_fields") or [])
    rf_ids = [f.get("id") for f in required if isinstance(f, dict) and f.get("id")]
    gaps = coverage_mod.required_field_gaps(session_id, rf_ids)
    rf_lookup = {f["id"]: f for f in required if isinstance(f, dict) and f.get("id")}
    field_captures: List[str] = []
    try:
        for fid in gaps:
            field = rf_lookup.get(fid) or {}
            prompt = field.get("prompt") or f"Tell me about: {fid}."
            for attempt in range(_REQUIRED_FIELD_TURN_CAP):
                owner_text = _prompt_owner(prompt, in_fn, turn)
                turn += 1
                new_ids = _process_owner_turn(
                    session_id, domain, turn, owner_text, tenant_id,
                    extra_keyword_tag=f"field:{fid}",
                )
                field_captures.extend(new_ids)
                if new_ids:
                    break
                # Re-ask once if the first attempt produced no row.
    except (EOFError, KeyboardInterrupt):
        return {
            "session_id": session_id,
            "domain": domain,
            "status": "paused",
            "captures": len(discovery_captures) + len(field_captures),
        }

    # ----- Stage D. Closing checkpoint -------------------------------------
    summary = _checkpoint_summary(tenant_id, session_id, domain)
    intent = ""
    try:
        for _ in range(_CHECKPOINT_TURN_CAP):
            print("\nHere's what I have so far:\n")
            print(summary)
            print(
                "\nIs this right? Reply: 'confirm' to lock it in, 'correct' to "
                "fix something, 'add' to add more, 'keep going' to extend the "
                "interview, or 'abandon' to drop this session."
            )
            owner_text = _prompt_owner("(intent)", in_fn, turn)
            turn += 1
            intent = _classify_intent(owner_text)
            if intent == "confirm":
                break
            if intent == "abandon":
                _mark_session(session_id, "abandoned")
                return {
                    "session_id": session_id,
                    "domain": domain,
                    "status": "abandoned",
                    "captures": len(discovery_captures) + len(field_captures),
                }
            # correct / add / keep_talking: run one more process pass.
            new_ids = _process_owner_turn(
                session_id, domain, turn, owner_text, tenant_id
            )
            field_captures.extend(new_ids)
            summary = _checkpoint_summary(tenant_id, session_id, domain)
        else:
            # Loop fell through without confirm — treat as confirm so we
            # don't lose the data over indecision.
            logger.info("Checkpoint turn-cap hit; auto-confirming.")
    except (EOFError, KeyboardInterrupt):
        return {
            "session_id": session_id,
            "domain": domain,
            "status": "paused",
            "captures": len(discovery_captures) + len(field_captures),
        }

    # ----- Stage E. Close ---------------------------------------------------
    out_path = _render_foundation(tenant_id, session_id, domain, ordinal, library)
    _mark_session(session_id, "complete")
    print(f"\nWrote foundation file: {out_path}")

    return {
        "session_id": session_id,
        "domain": domain,
        "status": "complete",
        "captures": len(discovery_captures) + len(field_captures),
        "foundation_path": str(out_path),
    }


def list_sessions() -> List[str]:
    return list(SESSION_DOMAINS.keys())


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv or sys.argv[1:])
    if not argv or argv[0] in ("list", "--list"):
        print("Available sessions:")
        for s in list_sessions():
            domain, _ = SESSION_DOMAINS[s]
            print(f"  {s}  →  domain={domain}")
        print("\nFlow:")
        print("  solomon onboard session_0   # industry (the floor)")
        print("  solomon onboard session_1   # belief_system")
        print("  ... continue through session_6")
        print("  solomon ingest path/to/old/material/*")
        print("  solomon ingestion review")
        return 0
    session_key = argv[0]
    try:
        result = run_session(session_key)
    except Exception as e:  # noqa: BLE001
        print(f"Onboarding session failed: {e}")
        return 1
    if session_key == "session_6" and result.get("status") == "complete":
        print("\n" + "─" * 60)
        print("Session 6 complete. The 7-session interview is done.")
        print("\nNext step:")
        print("  solomon ingest path/to/your/historical/files/*")
        print("\nThen review what was extracted:")
        print("  solomon ingestion review")
        print("\nAfter that, the brain enters observe-only mode for 30 days.")
        print("─" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

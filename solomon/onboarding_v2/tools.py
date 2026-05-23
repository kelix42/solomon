"""LLM-callable database tools for skill-driven onboarding.

The LLM follows the SKILL.md and calls these tools to:
  - Record what the owner said (``solomon_onboarding_capture``)
  - Check which required_fields are still unfilled (``solomon_onboarding_state``)
  - Mark the session complete and render the foundation YAML (``solomon_onboarding_complete``)
  - Abandon the session (``solomon_onboarding_abandon``)

The skill itself is the question-selection logic. These tools are
storage-only. They never propose questions, never editorialize, never
decide what to do next — they read and write rows.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..storage.pool import cursor, execute, get_conn, jsonify, parse_json
from . import session as session_mod

logger = logging.getLogger("solomon.onboarding_v2.tools")


PROBE_LIBRARY_DIR = Path(__file__).resolve().parent.parent / "onboarding" / "probe_library"


def _load_probe_library(domain: str) -> Optional[Dict[str, Any]]:
    """Read probe_library/<domain>.yaml as a dict."""
    path = PROBE_LIBRARY_DIR / f"{domain}.yaml"
    if not path.exists():
        # The yaml files use hyphens (belief-system.yaml) in the Drive
        # version; the shipped tree uses underscores. Try both.
        alt = PROBE_LIBRARY_DIR / f"{domain.replace('_', '-')}.yaml"
        if alt.exists():
            path = alt
        else:
            return None
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except Exception as e:  # noqa: BLE001
        logger.warning("could not load probe library %s: %s", path, e)
        return None


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_state(args: Dict[str, Any], **kw: Any) -> str:
    """Return JSON describing the current state of an open interview.

    Args: ``db_session_id`` (str, required).
    Returns JSON:
      {
        "session_id", "domain", "status", "turn_count", "items_captured",
        "required_fields": [{"id", "prompt", "filled", "latest_statement"}],
        "captures": [{"id", "type", "statement", "verbatim_phrase",
                      "field_tag", "keywords", "created_at"}, ...],
        "unfilled_count": int,
        "complete_ready": bool   # all required_fields are filled
      }
    """
    sid = args.get("db_session_id") or args.get("session_id")
    if not sid:
        return json.dumps({"error": "missing db_session_id"})
    try:
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "SELECT session_id, tenant_id, domain, status, items_captured, turn_count "
                    "FROM sessions WHERE session_id=?",
                    (sid,),
                )
                row = cur.fetchone()
                if row is None:
                    return json.dumps({"error": f"no session row for {sid}"})
                if hasattr(row, "keys"):
                    sess_id, tenant, domain, status, items, turns = (
                        row["session_id"], row["tenant_id"], row["domain"],
                        row["status"], row["items_captured"], row["turn_count"],
                    )
                else:
                    sess_id, tenant, domain, status, items, turns = row

                # Captured items for this session.
                execute(
                    cur,
                    "SELECT id, type, statement, verbatim_phrase, keywords, created_at "
                    "FROM captured_items "
                    "WHERE tenant_id=? AND session_id=? "
                    "ORDER BY created_at ASC",
                    (tenant, sess_id),
                )
                captures: List[Dict[str, Any]] = []
                # Map field_id -> latest captured statement (for required_fields filled check).
                latest_per_field: Dict[str, str] = {}
                for r in cur.fetchall():
                    if hasattr(r, "keys"):
                        cid, ctype, statement, verbatim, kw_raw, created = (
                            r["id"], r["type"], r["statement"],
                            r["verbatim_phrase"], r["keywords"], r["created_at"],
                        )
                    else:
                        cid, ctype, statement, verbatim, kw_raw, created = r
                    kw_any = parse_json(kw_raw) or []
                    kws: List[Any] = kw_any if isinstance(kw_any, list) else []
                    field_tag: Optional[str] = None
                    for k in kws:
                        if isinstance(k, str) and k.startswith("field:"):
                            field_tag = k.split(":", 1)[1]
                            latest_per_field[field_tag] = statement
                            break
                    captures.append({
                        "id": cid,
                        "type": ctype,
                        "statement": statement,
                        "verbatim_phrase": verbatim,
                        "field_tag": field_tag,
                        "keywords": kws,
                        "created_at": created,
                    })

        lib = _load_probe_library(domain) or {}
        rf_defs = lib.get("required_fields") or []
        required_fields_out: List[Dict[str, Any]] = []
        unfilled = 0
        for rf in rf_defs:
            if not isinstance(rf, dict):
                continue
            fid = rf.get("id")
            prompt = rf.get("prompt", "")
            filled = fid in latest_per_field
            if not filled:
                unfilled += 1
            required_fields_out.append({
                "id": fid,
                "prompt": prompt,
                "filled": filled,
                "latest_statement": latest_per_field.get(fid),
            })

        return json.dumps({
            "session_id": sess_id,
            "domain": domain,
            "status": status,
            "turn_count": turns,
            "items_captured": items,
            "required_fields": required_fields_out,
            "captures": captures,
            "unfilled_count": unfilled,
            "complete_ready": unfilled == 0 and len(required_fields_out) > 0,
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("solomon_onboarding_state failed")
        return json.dumps({"error": str(e)})


def _tool_capture(args: Dict[str, Any], **kw: Any) -> str:
    """Write a captured_items row.

    Args:
      ``db_session_id`` (str, required)
      ``statement`` (str, required) — your concise paraphrase of the owner's point
      ``verbatim_phrase`` (str, required) — the owner's actual words
      ``type`` (str, required) — one of: belief, principle, non_negotiable,
        preference, rule, example, constraint, metric, vocabulary
      ``field_id`` (str, optional) — required_field id this capture satisfies
      ``keywords`` (list[str], optional) — additional keyword tags
      ``example`` (str, optional)
      ``confidence`` (str, optional) — stated|repeated|exemplified
    """
    try:
        sid = args.get("db_session_id") or args.get("session_id")
        if not sid:
            return json.dumps({"error": "missing db_session_id"})
        statement = (args.get("statement") or "").strip()
        verbatim = (args.get("verbatim_phrase") or "").strip()
        ctype = (args.get("type") or "preference").strip()
        if not statement or not verbatim:
            return json.dumps({"error": "statement and verbatim_phrase are required"})

        # Look up domain + tenant from sessions row.
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(cur, "SELECT tenant_id, domain FROM sessions WHERE session_id=?", (sid,))
                row = cur.fetchone()
                if row is None:
                    return json.dumps({"error": f"no session row for {sid}"})
                if hasattr(row, "keys"):
                    tenant, domain = row["tenant_id"], row["domain"]
                else:
                    tenant, domain = row[0], row[1]

            keywords: List[str] = []
            extra_kw = args.get("keywords")
            if isinstance(extra_kw, list):
                keywords.extend(str(k) for k in extra_kw if k)
            field_id = args.get("field_id")
            if field_id:
                tag = f"field:{str(field_id)}"
                if tag not in keywords:
                    keywords.append(tag)

            new_id = uuid.uuid4().hex
            example = args.get("example")
            confidence = args.get("confidence", "stated")

            with cursor(conn) as cur:
                execute(
                    cur,
                    "INSERT INTO captured_items "
                    "(id, tenant_id, session_id, domain, type, statement, verbatim_phrase, "
                    " example, keywords, confidence, source_session, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                    (new_id, tenant, sid, domain, ctype, statement, verbatim,
                     example, jsonify(keywords), confidence, sid),
                )
                execute(
                    cur,
                    "UPDATE sessions "
                    "SET items_captured = items_captured + 1, turn_count = turn_count + 1 "
                    "WHERE session_id=?",
                    (sid,),
                )
            conn.commit()

        return json.dumps({
            "status": "captured",
            "id": new_id,
            "field_id": field_id,
            "keywords": keywords,
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("solomon_onboarding_capture failed")
        return json.dumps({"error": str(e)})


def _tool_complete(args: Dict[str, Any], **kw: Any) -> str:
    """Render the foundation YAML and mark the session complete.

    Args:
      ``db_session_id`` (str, required)
      ``force`` (bool, optional) — render even if some required_fields are
        unfilled (the YAML will have nulls for those fields)
    """
    try:
        sid = args.get("db_session_id") or args.get("session_id")
        if not sid:
            return json.dumps({"error": "missing db_session_id"})
        force = bool(args.get("force", False))

        # State check.
        state_json = _tool_state({"db_session_id": sid})
        state = json.loads(state_json)
        if "error" in state:
            return state_json
        if not force and state.get("unfilled_count", 99) > 0:
            return json.dumps({
                "status": "not_ready",
                "unfilled_count": state["unfilled_count"],
                "required_fields": state["required_fields"],
                "hint": "Some required_fields are unfilled. Either ask the owner the remaining questions, or call again with force=true.",
            })

        # Render YAML via the v1 helper (already debugged).
        from ..onboarding.session_runner import _render_foundation
        lib = _load_probe_library(state["domain"]) or {}
        from .session import SESSION_KEY_TO_DOMAIN
        # ordinal = inverse lookup
        ordinal = 0
        for k, (d, n) in SESSION_KEY_TO_DOMAIN.items():
            if d == state["domain"]:
                ordinal = n
                break

        # Look up tenant_id for _render_foundation
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(cur, "SELECT tenant_id FROM sessions WHERE session_id=?", (sid,))
                r = cur.fetchone()
                tenant = (r[0] if not hasattr(r, "keys") else r["tenant_id"]) if r else "default"

        path = _render_foundation(tenant, sid, state["domain"], ordinal, lib)
        session_mod.complete(sid)

        return json.dumps({
            "status": "complete",
            "session_id": sid,
            "yaml_path": str(path),
            "items_captured": state.get("items_captured", 0),
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("solomon_onboarding_complete failed")
        return json.dumps({"error": str(e)})


def _tool_abandon(args: Dict[str, Any], **kw: Any) -> str:
    """Mark the session abandoned. Captured rows are preserved.

    Args: ``db_session_id`` (str, required)
    """
    sid = args.get("db_session_id") or args.get("session_id")
    if not sid:
        return json.dumps({"error": "missing db_session_id"})
    try:
        session_mod.abandon(sid)
        return json.dumps({"status": "abandoned", "session_id": sid})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})


def _tool_list(args: Dict[str, Any], **kw: Any) -> str:
    """List onboarding sessions for the active tenant.

    Args:
      ``status`` (str, optional) — 'open' | 'complete' | 'abandoned'.
        Default: 'open'.
    """
    status = (args.get("status") or "open").strip()
    try:
        tenant = session_mod.ensure_tenant()
        with get_conn() as conn:
            with cursor(conn) as cur:
                execute(
                    cur,
                    "SELECT session_id, domain, status, started_at, completed_at, "
                    "items_captured, turn_count "
                    "FROM sessions WHERE tenant_id=? AND mode='onboarding' AND status=? "
                    "ORDER BY started_at DESC",
                    (tenant, status),
                )
                out = []
                for r in cur.fetchall():
                    if hasattr(r, "keys"):
                        out.append({
                            "session_id": r["session_id"],
                            "domain": r["domain"],
                            "status": r["status"],
                            "started_at": r["started_at"],
                            "completed_at": r["completed_at"],
                            "items_captured": r["items_captured"],
                            "turn_count": r["turn_count"],
                        })
                    else:
                        out.append({
                            "session_id": r[0], "domain": r[1], "status": r[2],
                            "started_at": r[3], "completed_at": r[4],
                            "items_captured": r[5], "turn_count": r[6],
                        })
        return json.dumps({"sessions": out, "count": len(out), "tenant_id": tenant})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_tools(adapter) -> None:  # noqa: ANN001
    """Register all six onboarding tools with the Hermes adapter."""
    adapter.register_tool(
        name="solomon_onboarding_state",
        description=(
            "Read the current state of an open Solomon onboarding interview. "
            "Returns the captured items so far, which required_fields are "
            "filled vs unfilled (with the prompt for each), and whether the "
            "session is ready to complete. Call this before deciding the "
            "next question, and again after each capture."
        ),
        parameters={
            "type": "object",
            "properties": {
                "db_session_id": {
                    "type": "string",
                    "description": "The Solomon database session id (e.g. 'onboarding-00-industry-20260523').",
                },
            },
            "required": ["db_session_id"],
        },
        handler=_tool_state,
    )

    adapter.register_tool(
        name="solomon_onboarding_capture",
        description=(
            "Write one captured_items row recording something the owner just told you. "
            "Pass field_id when the capture fills one of the required_fields from "
            "solomon_onboarding_state. statement is your concise paraphrase. "
            "verbatim_phrase is the owner's exact words (preserve them — they're "
            "the raw data). Use type='preference' as the default; pick a more "
            "specific type (belief, principle, non_negotiable, rule, etc.) when "
            "the content fits."
        ),
        parameters={
            "type": "object",
            "properties": {
                "db_session_id": {"type": "string"},
                "statement": {"type": "string", "description": "Concise paraphrase of the owner's point."},
                "verbatim_phrase": {"type": "string", "description": "The owner's exact words."},
                "type": {
                    "type": "string",
                    "description": "One of: belief, principle, non_negotiable, preference, rule, example, constraint, metric, vocabulary.",
                },
                "field_id": {
                    "type": "string",
                    "description": "Optional. The required_field id this capture satisfies (from solomon_onboarding_state).",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional. Extra keyword tags.",
                },
                "example": {"type": "string"},
                "confidence": {
                    "type": "string",
                    "description": "stated | repeated | exemplified",
                },
            },
            "required": ["db_session_id", "statement", "verbatim_phrase", "type"],
        },
        handler=_tool_capture,
    )

    adapter.register_tool(
        name="solomon_onboarding_complete",
        description=(
            "Mark the session complete and render the foundation YAML file. "
            "Will refuse unless all required_fields are filled (or force=true). "
            "Call this only after the closing-checkpoint summary has been read "
            "back to the owner and they confirmed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "db_session_id": {"type": "string"},
                "force": {
                    "type": "boolean",
                    "description": "Render even if some required_fields are unfilled.",
                },
            },
            "required": ["db_session_id"],
        },
        handler=_tool_complete,
    )

    adapter.register_tool(
        name="solomon_onboarding_abandon",
        description=(
            "Mark the session abandoned. Captured rows are preserved; the "
            "session row's status flips to 'abandoned'. Use only when the "
            "owner explicitly says they want to stop."
        ),
        parameters={
            "type": "object",
            "properties": {
                "db_session_id": {"type": "string"},
            },
            "required": ["db_session_id"],
        },
        handler=_tool_abandon,
    )

    adapter.register_tool(
        name="solomon_onboarding_list",
        description="List Solomon onboarding sessions for the active tenant, filtered by status.",
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "open | complete | abandoned. Default: open.",
                },
            },
        },
        handler=_tool_list,
    )

"""Shared helpers for the pipeline-stage tests.

Each stage's test file imports from here so we don't duplicate the
events-row seed + stub-LLM scaffolding across six files.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from solomon.storage.pool import cursor, execute, get_conn


# ---------------------------------------------------------------------------
# Events row seeding
# ---------------------------------------------------------------------------

def seed_event(
    event_id: str = "ev-test-1",
    *,
    tenant_id: str = "default",
    source: str = "telegram",
    raw_content: str = "should I send a quote at 18% margin",
    salience_score: Optional[float] = None,
    classification: Optional[dict] = None,
    retrieval_context: Optional[dict] = None,
    system1_output: Optional[Any] = None,
    system2_output: Optional[Any] = None,
    divergence_score: Optional[float] = None,
    audit_verdict: Optional[str] = None,
    audit_reasoning: Optional[str] = None,
    owner_state: Optional[str] = None,
    owner_state_ceiling: Optional[int] = None,
    effective_autonomy: Optional[int] = None,
    action_taken: Optional[str] = None,
    status: str = "pending",
) -> None:
    """Insert one events row with the given columns. Caller controls
    every field; defaults give a non-private telegram message."""

    def _maybe_json(v):
        if v is None:
            return None
        if isinstance(v, (dict, list)):
            return json.dumps(v)
        return v

    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO events ("
                "  event_id, tenant_id, source, received_at, participants, "
                "  raw_content, channel_metadata, salience_score, classification, "
                "  retrieval_context, system1_output, system2_output, "
                "  divergence_score, audit_verdict, audit_reasoning, "
                "  owner_state, owner_state_ceiling, effective_autonomy, "
                "  action_taken, status"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id, tenant_id, source,
                    datetime.now(timezone.utc).isoformat(),
                    "[]", raw_content, "{}",
                    salience_score,
                    _maybe_json(classification),
                    _maybe_json(retrieval_context),
                    _maybe_json(system1_output) if not isinstance(system1_output, str) else system1_output,
                    _maybe_json(system2_output) if not isinstance(system2_output, str) else system2_output,
                    divergence_score, audit_verdict, audit_reasoning,
                    owner_state, owner_state_ceiling, effective_autonomy,
                    action_taken, status,
                ),
            )
        conn.commit()


def read_event(event_id: str) -> Dict[str, Any]:
    """Return the events row as a dict."""
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT * FROM events WHERE event_id = ?", (event_id,))
            row = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], row))


# ---------------------------------------------------------------------------
# Stub LLM client
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, text: str):
        self.text = text
        self.model = "stub"
        self.tokens_in = 0
        self.tokens_out = 0


class StubLLM:
    """LLMClient stub that routes by system-prompt content.

    Pattern from tests/test_session_runner.py. Subclasses register
    responders via ``add(matcher, responder)``; the matcher is a
    callable taking the kwargs dict, returning bool.
    """

    configured = True

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self._routes: List[tuple] = []

    def add(self, matcher: Callable[[Dict[str, Any]], bool], responder):
        """Register a (matcher, responder) pair. Responder is either a
        str (returned verbatim as resp.text) or callable(kwargs) → str.
        """
        self._routes.append((matcher, responder))

    def call(self, *, tier, system, user, json_mode=False, max_tokens=1024, temperature=0.2):
        kwargs = {
            "tier": tier,
            "system": system,
            "user": user,
            "json_mode": json_mode,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        self.calls.append(kwargs)
        for matcher, responder in self._routes:
            if matcher(kwargs):
                text = responder(kwargs) if callable(responder) else str(responder)
                return _Resp(text)
        return _Resp("")

    @staticmethod
    def parse_json(text):
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None


def install_stub_llm(monkeypatch, stub: Optional[StubLLM] = None) -> StubLLM:
    """Replace solomon.reasoning.llm._client + get_client with ``stub``."""
    if stub is None:
        stub = StubLLM()
    from solomon.reasoning import llm as llm_mod
    monkeypatch.setattr(llm_mod, "_client", stub)
    monkeypatch.setattr(llm_mod, "get_client", lambda: stub)
    return stub


def reset_tenant_cache():
    """Some stages call get_or_create_tenant_id; reset between tests."""
    from solomon.storage import decisions
    decisions.reset_tenant_cache()

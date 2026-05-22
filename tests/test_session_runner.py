"""Integration test for solomon.onboarding.session_runner.

Drives one full session_0 (industry) run end-to-end with a stubbed LLM
client and a scripted input stream. Verifies:
  - session row is created and ends in 'complete'
  - captured_items rows landed
  - required-field tags were applied
  - foundation YAML was written and parses
"""

import json
from pathlib import Path

import pytest
import yaml

from solomon.onboarding import session_runner
from solomon.onboarding.interview import engine as engine_mod
from solomon.reasoning import llm as llm_mod
from solomon.storage import pool


class _StubLLMClient:
    """Pretends to be solomon.reasoning.llm.LLMClient. Returns a canned
    response per call tier so the session runs deterministically.

    - tier='fast' with EXTRACTION_SYSTEM-flavoured prompt → one captured item
      with the owner's verbatim phrase and a 'industry' keyword.
    - tier='fast' with CONTRADICTION_SYSTEM-flavoured prompt → no conflict.
    - tier='fast' with intent classifier prompt → confirm.
    - Anything else → empty.
    """

    configured = True

    def __init__(self):
        self.calls = []

    def call(self, *, tier, system, user, json_mode=False, max_tokens=1024,
             temperature=0.2):
        self.calls.append({"tier": tier, "system": system[:80], "user": user[:80]})

        # Intent classifier: short system mentioning 'Classify the owner'
        if "Classify the owner" in system:
            return _Resp(json.dumps({"intent": "confirm"}))

        # Contradiction check: system mentions 'contradiction'
        if "contradiction" in system.lower():
            return _Resp(json.dumps({"is_conflict": False, "reason": "", "suggested_probe": ""}))

        # Idiom pass for vocabulary
        if "idioms" in system.lower():
            return _Resp(json.dumps({"phrases": []}))

        # Otherwise: assume extraction. Echo the owner's verbatim phrase.
        # Grab the literal text the owner just said from the user prompt.
        verbatim = "the owner said something"
        marker = 'Owner just said:'
        if marker in user:
            block = user.split(marker, 1)[1]
            # The block is wrapped in triple quotes.
            inner = block.split('"""', 2)
            if len(inner) >= 2:
                verbatim = inner[1].strip()
        item = {
            "type": "preference",
            "statement": f"Owner stated: {verbatim[:80]}",
            "verbatim_phrase": verbatim[:120] or "stub",
            "example": None,
            "keywords": ["product", "industry"],
            "confidence": "stated",
        }
        return _Resp(json.dumps({"items": [item]}))

    @staticmethod
    def parse_json(text):
        try:
            return json.loads(text)
        except Exception:
            return None


class _Resp:
    def __init__(self, text):
        self.text = text
        self.model = "stub"
        self.tokens_in = 0
        self.tokens_out = 0


@pytest.fixture
def stub_llm(monkeypatch):
    """Inject the stub LLM client into the singleton getter."""
    stub = _StubLLMClient()
    monkeypatch.setattr(llm_mod, "_client", stub)
    monkeypatch.setattr(llm_mod, "get_client", lambda: stub)
    # The interview modules grabbed get_client at import time, but each
    # call site goes through llm_mod.get_client() at call time.
    yield stub
    monkeypatch.setattr(llm_mod, "_client", None)


@pytest.fixture
def foundation_dir(monkeypatch, tmp_path):
    """Redirect the foundation output dir to tmp."""
    target = tmp_path / "foundation"
    monkeypatch.setattr(session_runner, "FOUNDATION_DIR", target)
    return target


def _scripted_input(answers):
    """Build an input_fn that returns the next item from `answers` each call."""
    it = iter(answers)
    def _inner(prompt):
        try:
            return next(it)
        except StopIteration:
            # If the test underestimated turn count, raise EOF (like Ctrl-D)
            # so the runner exits cleanly into the 'paused' branch.
            raise EOFError
    return _inner


def test_session_0_industry_runs_to_completion(
    solomon_db, stub_llm, foundation_dir
):
    """Drive session_0 through Stage A→E with a small set of answers."""
    engine_mod._LIBRARY_CACHE.clear()

    # Stub responses cover discovery + required fields + 1 confirm.
    answers = [
        # Discovery turns. The stub returns 1 capture per turn with keywords
        # ['product', 'industry']; coverage has rows seeded for every probe
        # library keyword. The dry-streak rule will only fire after 6+ probes
        # produce no captures — every turn here produces a capture so it
        # won't fire. We use /done to short-circuit discovery for the test.
        "We make custom landfill liners for municipal clients.",
        "Mostly municipal customers in Manitoba and Saskatchewan.",
        "/done",
        # Required-fields pass: industry library has 7 required_fields,
        # each gets up to 2 attempts. Provide a one-line answer per field.
        "construction services",      # business_category
        "engineered landfill liners", # primary_product_or_service
        "mostly businesses",          # customer_orientation
        "regional, western Canada",   # geographic_scope
        "project by project",         # revenue_model
        "established",                # growth_stage
        "no, well diversified",       # concentration_risk
        # Stage D closing checkpoint: confirm.
        "confirm",
    ]

    result = session_runner.run_session(
        "session_0",
        input_fn=_scripted_input(answers),
        max_discovery_turns=10,
    )

    assert result["status"] == "complete", result
    assert result["domain"] == "industry"
    assert result["captures"] >= 7  # at least the seven required-field rows

    # The session row should be marked complete.
    with pool.get_conn() as conn:
        with pool.cursor(conn) as cur:
            pool.execute(
                cur,
                "SELECT status, items_captured FROM sessions WHERE session_id=?",
                (result["session_id"],),
            )
            row = cur.fetchone()
    assert row[0] == "complete"
    assert int(row[1]) >= 7

    # Each required-field tag should appear on at least one captured row.
    field_ids = [
        "business_category", "primary_product_or_service",
        "customer_orientation", "geographic_scope",
        "revenue_model", "growth_stage", "concentration_risk",
    ]
    with pool.get_conn() as conn:
        with pool.cursor(conn) as cur:
            for fid in field_ids:
                pool.execute(
                    cur,
                    "SELECT COUNT(*) FROM captured_items "
                    "WHERE session_id=? AND keywords LIKE ?",
                    (result["session_id"], f'%"field:{fid}"%'),
                )
                count = cur.fetchone()[0]
                assert int(count) >= 1, f"required field {fid} was not captured"

    # The foundation YAML should be readable and have the right shape.
    out_path = Path(result["foundation_path"])
    assert out_path.exists()
    doc = yaml.safe_load(out_path.read_text())
    assert doc["domain"] == "industry"
    assert doc["session_id"] == result["session_id"]
    assert isinstance(doc["required_fields"], dict)
    assert set(doc["required_fields"].keys()) == set(field_ids)
    assert isinstance(doc["discovery"], list)


def test_session_0_unknown_key_raises(solomon_db):
    with pytest.raises(ValueError, match="Unknown session"):
        session_runner.run_session("session_99", input_fn=lambda _: "")


def test_list_sessions_contains_industry_first():
    sessions = session_runner.list_sessions()
    assert sessions[0] == "session_0"
    assert "session_6" in sessions

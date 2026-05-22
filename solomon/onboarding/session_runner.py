"""Onboarding session runner (Part 25).

Drives the structured 6-session interview that fills the tenant's
foundation files. Each session is loaded from
solomon/onboarding/curriculum/sessions.yaml.

The actual conversation happens through Hermes — this module exposes a
CLI entry point (``solomon onboard``) that walks the user through one
session at a time, transcribes voice if provided, asks follow-ups via
the deep LLM, and writes the resulting YAML to
``~/.hermes/solomon/foundation/``.

For Phase 1 the runner supports text-only input and writes draft YAML
files. Voice transcription via Whisper and the in-Hermes session UX are
planned for Phase 2.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..reasoning.llm import get_client

logger = logging.getLogger("solomon.onboarding")

FOUNDATION_DIR = Path(os.path.expanduser("~/.hermes/solomon/foundation"))
TAXONOMY_DIR = Path(os.path.expanduser("~/.hermes/solomon/taxonomy"))
CURRICULUM_FILE = Path(__file__).parent / "curriculum" / "sessions.yaml"


def load_curriculum() -> Dict[str, Any]:
    with open(CURRICULUM_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def list_sessions() -> List[str]:
    cur = load_curriculum()
    return sorted([k for k in cur.keys() if k.startswith("session_")])


def run_session(session_key: str, mode: str = "text") -> Dict[str, Any]:
    """Run a single session interactively.

    For Phase 1 this reads questions from stdin/stdout. Mode 'text' is
    the only supported mode; 'voice' is planned.
    """
    cur = load_curriculum()
    if session_key not in cur:
        raise ValueError(f"Unknown session: {session_key}. Available: {list_sessions()}")
    session = cur[session_key]
    print(f"\n=== {session['name']} ({session.get('duration_min', 60)} min) ===\n")
    answers: List[Dict[str, str]] = []
    for q in session.get("questions", []):
        print(f"\n> {q}")
        try:
            answer = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n(session interrupted)")
            break
        answers.append({"q": q, "a": answer})
        # Phase 2: ask a follow-up via the deep LLM after each answer.

    output = _structure_answers(session, answers)
    _write_output(session, output, session_key)
    return output


def _structure_answers(session: Dict[str, Any], answers: List[Dict[str, str]]) -> Dict[str, Any]:
    """Ask the deep LLM to convert raw Q&A into structured YAML matching
    the session's output format.
    """
    client = get_client()
    if not client.configured:
        logger.warning("LLM not configured; storing raw Q&A only.")
        return {"raw_qa": answers, "structured": None}
    schema_hint = session.get("format_note", "Structure the owner's answers into a clean YAML document.")
    user_prompt = (
        f"Session: {session['name']}\n\n"
        f"Q&A:\n{json.dumps(answers, indent=2)}\n\n"
        f"Output format: {schema_hint}\n\n"
        f"Return a JSON object with a single key 'document' containing the structured YAML "
        f"(as a JSON-encoded object). The owner will review the diff before commit."
    )
    resp = client.call(
        tier="onboarding",
        system="You are Solomon's onboarding assistant. Convert the owner's answers into a clean structured document matching the requested format. Do not invent content the owner did not say.",
        user=user_prompt,
        json_mode=True,
        max_tokens=2048,
        temperature=0.2,
    )
    parsed = client.parse_json(resp.text) or {}
    return {"raw_qa": answers, "structured": parsed.get("document"), "model": resp.model}


def _write_output(session: Dict[str, Any], output: Dict[str, Any], session_key: str) -> None:
    targets: List[str] = []
    if "output_file" in session:
        targets.append(session["output_file"])
    if "output_files" in session:
        targets.extend(session["output_files"])
    FOUNDATION_DIR.mkdir(parents=True, exist_ok=True)
    TAXONOMY_DIR.mkdir(parents=True, exist_ok=True)
    base = Path(os.path.expanduser("~/.hermes/solomon"))
    for rel in targets:
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "session": session_key,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "document": output.get("structured"),
                    "raw_qa": output.get("raw_qa"),
                },
                f,
                sort_keys=False,
            )
        print(f"\nWrote {path}")


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv or sys.argv[1:])
    if not argv or argv[0] in ("list", "--list"):
        print("Available sessions:")
        for s in list_sessions():
            print(f"  {s}")
        return 0
    session_key = argv[0]
    try:
        run_session(session_key)
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"Onboarding session failed: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

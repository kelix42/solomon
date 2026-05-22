"""Stage 4 — Hard-rule (deterministic, no LLM).

Drive source: ``orchestrator/pipeline/stage_hard_rule.py`` lines 22-25
(`from json_logic import jsonLogic`). Report §3 line 43 — "Reads
``foundation/05-non-negotiables.yaml`` ``rules:`` list; each has a
JSON-logic ``condition`` evaluated via ``json_logic.jsonLogic(condition,
data)``. On match → ``status='blocked_by_hard_rule'``, halts."

Order matters: this is Stage 4 (after salience+classification, BEFORE
retrieval+S1+S2+audit). A violation here saves three downstream LLM
calls and prevents the brain from speaking before the hard rule fires.

JSON-logic ``data`` shape is intentionally ``{"event": <full event row>}``
so rules can reference structured columns:

    {"and": [
        {"==": [{"var": "event.classification.scope"}, "pricing"]},
        {"<":  [{"var": "event.payload.margin_pct"}, 15]}
    ]}
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._helpers import set_event_status, update_event

logger = logging.getLogger("solomon.pipeline.hard_rule")

# Default path resolution: look in repo foundation/ first (for dev / tests),
# then ~/.hermes/solomon/foundation/ (the installed location).
_REPO_FOUNDATION = Path(__file__).resolve().parents[2] / "foundation" / "05-non-negotiables.yaml"
_HOME_FOUNDATION = Path(
    os.path.expanduser(
        os.getenv(
            "SOLOMON_NON_NEGOTIABLES_PATH",
            "~/.hermes/solomon/foundation/05-non-negotiables.yaml",
        )
    )
)


def _candidate_paths() -> List[Path]:
    # Explicit env var always wins.
    env = os.getenv("SOLOMON_NON_NEGOTIABLES_PATH")
    if env:
        return [Path(os.path.expanduser(env))]
    return [_HOME_FOUNDATION, _REPO_FOUNDATION]


def _load_rules() -> List[Dict[str, Any]]:
    """Read rules from foundation/05-non-negotiables.yaml. Returns [] on miss."""
    try:
        import yaml  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.warning("PyYAML unavailable, hard-rule stage is a no-op: %s", e)
        return []

    for path in _candidate_paths():
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to load %s: %s", path, e)
            continue

        if isinstance(data, list):
            rules = data
        elif isinstance(data, dict):
            rules = data.get("rules") or data.get("non_negotiables") or []
        else:
            rules = []

        if not isinstance(rules, list):
            logger.warning("Non-negotiables in %s must be a list; got %s", path, type(rules).__name__)
            return []
        return [r for r in rules if isinstance(r, dict)]
    return []


def evaluate_rules(rules: List[Dict[str, Any]], data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the first rule whose ``condition`` matches ``data``, else None.

    Pulled out as a module-level function so the synchronous shim in
    ``solomon.non_negotiables.check`` can reuse it without involving the
    events table.
    """
    try:
        from json_logic import jsonLogic  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.warning("json_logic import failed; hard-rule stage is a no-op: %s", e)
        return None

    for rule in rules:
        cond = rule.get("condition")
        if not cond:
            continue
        try:
            verdict = jsonLogic(cond, data)
        except Exception as e:  # noqa: BLE001
            logger.warning("json_logic eval failed for rule %r: %s", rule.get("id"), e)
            continue
        if verdict:
            return rule
    return None


def run(event_id: str, event_row: dict) -> dict:
    """Evaluate non-negotiables. Block the pipeline on any match.

    Mutates ``event_row`` in place: sets ``hard_rule_verdict``, optionally
    ``hard_rule_reason``, optionally ``status``.
    """
    rules = _load_rules()
    if not rules:
        # No rules loaded → automatic pass. Don't blow up.
        update_event(event_id, hard_rule_verdict="pass")
        event_row["hard_rule_verdict"] = "pass"
        return event_row

    matched = evaluate_rules(rules, {"event": event_row})
    if matched is None:
        update_event(event_id, hard_rule_verdict="pass")
        event_row["hard_rule_verdict"] = "pass"
        return event_row

    on_violate = matched.get("on_violate") or {}
    explanation = on_violate.get("explanation") or matched.get("statement") or "non-negotiable rule matched"
    rule_id = matched.get("id") or matched.get("name") or "<anonymous>"
    logger.warning("stage_hard_rule: event %s blocked by rule %s — %s", event_id, rule_id, explanation)

    update_event(
        event_id,
        hard_rule_verdict="block",
        hard_rule_reason=f"{rule_id}: {explanation}",
    )
    set_event_status(event_id, "blocked_by_hard_rule", reason=explanation)
    event_row["hard_rule_verdict"] = "block"
    event_row["hard_rule_reason"] = f"{rule_id}: {explanation}"
    event_row["status"] = "blocked_by_hard_rule"
    return event_row

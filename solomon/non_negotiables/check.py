"""Non-negotiable checker.

Non-negotiables are hard rules the brain will NEVER violate — things like
"never work on Sundays" or "never email my ex". They are tenant-defined,
stored in `non_negotiables.yaml` in the tenant's foundation directory
(eventually a GitHub-synced repo; read from disk for now), and consulted
on every event before any autonomous action is considered.

A single matched rule short-circuits the rest of the pipeline: the
conductor turns the event into a no-op (or escalates to the user) instead
of letting Solomon act on it. This is the safety floor — it sits below
the audit gate and below user preferences.

See Part 6 of the design doc.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..reasoning.llm import get_client

logger = logging.getLogger("solomon.non_negotiables")

NON_NEGOTIABLES_PATH = os.path.expanduser(
    "~/.hermes/solomon/foundation/non_negotiables.yaml"
)


@dataclass
class NonNegotiableViolation:
    rule_name: str
    reason: str
    scope: Optional[str] = None


class NonNegotiableChecker:
    """Checks each event against the tenant's non-negotiables.

    Loads rules from disk at construction time. If the file is missing or
    malformed, the checker becomes a no-op (every `check()` returns None)
    rather than crashing the conductor — better to lose a safety check
    than to wedge the brain entirely. The conductor logs visibility
    elsewhere; here we just warn.
    """

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.rules: List[Dict[str, Any]] = self._load_rules()

    # ---- loading ---------------------------------------------------------

    def _load_rules(self) -> List[Dict[str, Any]]:
        path = NON_NEGOTIABLES_PATH
        if not os.path.exists(path):
            logger.info(
                "No non_negotiables.yaml at %s; non-negotiable checker is a no-op.",
                path,
            )
            return []

        try:
            import yaml  # local import — yaml is optional at import time
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "PyYAML not available, cannot load non-negotiables: %s. "
                "Checker is a no-op.",
                e,
            )
            return []

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or []
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to load non_negotiables.yaml at %s: %s. "
                "Checker is a no-op.",
                path,
                e,
            )
            return []

        # Accept either a top-level list, or a dict with a 'rules' key.
        if isinstance(data, dict):
            data = data.get("rules") or data.get("non_negotiables") or []
        if not isinstance(data, list):
            logger.warning(
                "non_negotiables.yaml must be a list (or {rules: [...]}); "
                "got %s. Checker is a no-op.",
                type(data).__name__,
            )
            return []

        valid: List[Dict[str, Any]] = []
        for i, rule in enumerate(data):
            if not isinstance(rule, dict):
                logger.warning("Skipping non-negotiable #%d: not a mapping.", i)
                continue
            name = rule.get("name")
            check_type = rule.get("check_type")
            pattern = rule.get("check_pattern")
            if not name or check_type not in {"keyword", "llm", "regex"} or pattern is None:
                logger.warning(
                    "Skipping non-negotiable #%d (name=%r): missing/invalid "
                    "name/check_type/check_pattern.",
                    i,
                    name,
                )
                continue
            valid.append(rule)

        logger.info("Loaded %d non-negotiable rule(s) from %s.", len(valid), path)
        return valid

    # ---- checking --------------------------------------------------------

    def check(
        self,
        raw_event: Any,
        scope: Optional[str] = None,
    ) -> Optional[NonNegotiableViolation]:
        """Return the first violation found, or None if the event is clean.

        `scope` is the current action scope ('email', 'calendar', etc.) —
        rules with a matching scope (or scope='all') are evaluated. Rules
        without a scope are treated as 'all'.
        """
        if not self.rules:
            return None

        content = getattr(raw_event, "raw_content", "") or ""

        for rule in self.rules:
            rule_scope = rule.get("scope", "all") or "all"
            if rule_scope != "all" and scope is not None and rule_scope != scope:
                continue

            check_type = rule["check_type"]
            pattern = rule["check_pattern"]
            name = rule["name"]
            description = rule.get("description", "")

            try:
                violation = self._evaluate(check_type, pattern, content, description)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Non-negotiable %r raised during evaluation: %s. "
                    "Treating as no-match.",
                    name,
                    e,
                )
                continue

            if violation is not None:
                logger.info(
                    "Non-negotiable violated: rule=%r scope=%r reason=%s",
                    name,
                    rule_scope,
                    violation,
                )
                return NonNegotiableViolation(
                    rule_name=name,
                    reason=violation,
                    scope=rule_scope if rule_scope != "all" else None,
                )

        return None

    # ---- per-type evaluation --------------------------------------------

    def _evaluate(
        self,
        check_type: str,
        pattern: Any,
        content: str,
        description: str,
    ) -> Optional[str]:
        """Return a reason string if this rule fires, else None."""
        if check_type == "keyword":
            return self._check_keyword(pattern, content)
        if check_type == "regex":
            return self._check_regex(pattern, content)
        if check_type == "llm":
            return self._check_llm(pattern, content, description)
        return None

    @staticmethod
    def _check_keyword(pattern: Any, content: str) -> Optional[str]:
        # pattern may be a single string or a list of strings.
        keywords: List[str]
        if isinstance(pattern, str):
            keywords = [pattern]
        elif isinstance(pattern, list):
            keywords = [str(p) for p in pattern]
        else:
            return None

        lower = content.lower()
        for kw in keywords:
            if not kw:
                continue
            if kw.lower() in lower:
                return f"matched keyword: {kw!r}"
        return None

    @staticmethod
    def _check_regex(pattern: Any, content: str) -> Optional[str]:
        if not isinstance(pattern, str):
            return None
        m = re.search(pattern, content)
        if m:
            return f"matched regex {pattern!r} at {m.group(0)!r}"
        return None

    @staticmethod
    def _check_llm(pattern: Any, content: str, description: str) -> Optional[str]:
        if not isinstance(pattern, str) or not pattern.strip():
            return None
        client = get_client()
        if not client.configured:
            logger.warning(
                "LLM non-negotiable skipped: Solomon LLM client not configured."
            )
            return None

        user_prompt = (
            f"{pattern}\n\n"
            "Event content:\n"
            f"---\n{content}\n---\n\n"
            'Does this event suggest the rule will be violated? '
            'Respond JSON {"violation": true|false, "reason": str}'
        )
        resp = client.call(
            tier="deep",
            system=(
                "You are a safety check for a personal AI assistant. "
                "You evaluate whether an incoming event would cause a hard "
                "rule to be violated. Be conservative: only flag clear "
                "violations. Always respond with valid JSON."
            ),
            user=user_prompt,
            json_mode=True,
            max_tokens=256,
            temperature=0.0,
        )
        parsed = client.parse_json(resp.text)
        if not parsed:
            return None
        if bool(parsed.get("violation")):
            reason = str(parsed.get("reason") or description or "LLM flagged violation")
            return reason
        return None

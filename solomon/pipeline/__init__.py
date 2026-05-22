"""Solomon's 10-stage decision pipeline.

Drive source: ``orchestrator/pipeline/`` package (port of stage_*.py modules).
Report ref: REPORT-PIPELINE.md §3 — "10-stage pipeline order" and §4.1.

The pipeline reads ``db.events`` rows and walks ten stages in order. Each
stage updates one or more columns on the events row; the row IS the audit
trail (no separate per-stage tables).

Stage order (hard-rule deliberately at Stage 4, *before* retrieval and
the three expensive LLM calls):

  1. capture          — validates event row exists
  2. salience         — fast LLM; halt if < 0.30
  3. classification   — mid-tier LLM
  4. hard_rule        — JSON-logic, deterministic; halt on match
  5. retrieval        — working memory + 5-lane long-term
  6. system_1         — fast intuitive answer
  7. system_2         — deep deliberate answer + token-Jaccard divergence
  8. audit            — independent gate; APPROVE / DOWNGRADE / REJECT / REQUEST_RETHINK
  9. owner_state      — biometric ceiling (v1: returns 'unknown' → no ceiling)
 10. action           — effective_autonomy = min(scope_level, ceil); routes the action

Public API:
    >>> from solomon.pipeline import run
    >>> result = run(event_id="01HX...")
"""
from __future__ import annotations

from .runner import run

__all__ = ["run"]

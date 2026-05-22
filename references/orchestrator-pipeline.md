# Orchestrator Pipeline — distilled §2.2.5 of SOLOMON-PLAN.md

Real-time owner-on-behalf decisions flow through a deterministic 10-stage pipeline. Each external event becomes a `db.events` row and is processed in order. Failure of any stage stops the pipeline for that event; row records `status = failed`.

```
Capture → Salience → Classification → Hard-rule check → Working memory + 5-lane retrieval
       → System 1 (Sonnet) → System 2 (Opus) → Audit gate (Opus) → Owner-state gate → Action
```

## Stages

| # | Stage | Model | Input | Output |
|---|---|---|---|---|
| 1 | Capture | n/a | external event | `db.events` row with `status = pending` |
| 2 | Salience | Haiku | event payload | 0.0–1.0; < 0.30 → skip |
| 3 | Classification | Sonnet | payload + salience | `{scope, domain, decision_type}` |
| 4 | Hard-rule | none (deterministic) | event + classification + `foundation/05-non-negotiables.yaml` | APPROVE / `blocked_by_hard_rule` |
| 5 | Working memory + 5-lane retrieval | none (Pinecone) | classification | top-K context |
| 6 | System 1 | Sonnet | hot identity + context + rules | rule-based answer (1–2 sentences) |
| 7 | System 2 | Opus | same context + chain-of-thought | full reasoning + answer |
| 7b | Divergence check | none (token-Jaccard) | System 1 vs System 2 outputs | `divergence_score` 0.0–1.0; < 0.7 → mentoring_queue priority 4 |
| 8 | Audit gate | Opus | proposed_action + hard_rules + scope + owner_state + System 1/2 | APPROVE / DOWNGRADE / REJECT / REQUEST_RETHINK |
| 9 | Owner-state gate | none | `db.biometrics` latest | green/yellow/red ceiling |
| 10 | Action | n/a | `(effective_autonomy, audit_verdict)` | ship / one-tap / suggest / escalate |

## Per-stage timing

`db.events.stage_timings_ms` is a JSON object keyed by stage name. Used by `solomon-audit` to surface slow stages.

## Decision-log entry per event

Successful events (`status = complete`) write a row to `db.decisions` and an entry to `decisions/log.md`. The H2 title is the action; the body follows the canonical four-field format (§2.11).

## See also

- `references/system1-system2.md` — the System 1 / System 2 split
- `references/retrieval-5-lane.md` — Lane 1 queries 4 namespaces
- `references/autonomy-spectrum.md` — L0–L4 + owner-state ceiling
- `orchestrator/pipeline/` — Python skeletons for each stage

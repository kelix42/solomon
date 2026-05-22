# Architecture

Solomon turns Hermes into a domain-specific decision engine. This doc maps each part of the design doc to a Python module.

## The one-line story

Every Hermes turn flows through `Conductor.process_event()`. The conductor scores salience, classifies, pulls memory, predicts (S1), reasons (S2), audits, and logs. The user sees nothing different — Hermes still chats — but in the background a decision log is filling up, predictions are being made, and the brain is getting smarter.

## Module map (Part → file)

| Design doc part | Module |
|---|---|
| Part 2 — Capture | `solomon/capture/raw_event.py` |
| Part 3 — Salience | `solomon/salience/scorer.py` |
| Part 4 — Conductor | `solomon/conductor.py` |
| Part 5 — Classification | `solomon/classify/classifier.py` |
| Part 6 — Non-negotiables | `solomon/non_negotiables/check.py` |
| Part 7 — Working memory | `solomon/memory/working.py` |
| Part 8 — Multi-lane retrieval | `solomon/memory/retrieval.py` |
| Part 9 — Predict before reason | `solomon/reasoning/system_1.py`, `system_2.py`, `divergence.py` |
| Part 10 — Audit gate | `solomon/audit_gate/audit.py` |
| Part 11 — Autonomy ladder | `solomon/autonomy/ladder.py` |
| Part 12 — Decision logging | `solomon/storage/decisions.py` |
| Part 13 — Predictions / counterfactuals | `solomon/predictions/checkpoints.py`, `counterfactuals.py` |
| Part 14 — Mentoring sessions | (Phase 4) |
| Part 15 — Action layer | (delegated to Hermes tool calls + audit gate) |
| Part 16 — Sleep cycle | `solomon/sleep/runner.py` + 8 job files |
| Part 17 — Storage schema | `solomon/storage/schema.sql` |
| Part 18 — Per-tenant isolation | `solomon/storage/decisions.py::get_or_create_tenant_id` + row-level security (Phase 5) |
| Part 19 — Configuration | `~/.hermes/config.yaml` + `solomon/cli.py` |
| Part 20 — Failure modes | inline try/except + safe defaults |
| Part 21 — Build order | `docs/PHASES.md` |
| Part 22 — Glossary | inline docstrings |
| Part 24 — Heuristic lifecycle | `solomon/storage/schema.sql::heuristics` + Job 2 / Job 7 |
| Part 25 — Onboarding | `solomon/onboarding/` |
| Part 26 — Ingestion | `solomon/ingestion/` |
| Kill switch | `solomon/private/mode.py` |

## How Hermes and Solomon talk

Only one file knows Hermes internals: `solomon/adapter.py`. Everything else talks to the adapter.

The adapter uses the four pieces of the Hermes plugin contract:

1. `ctx.register_tool(...)` — Solomon registers its own tools (audit_gate, log_decision, etc.) into the Hermes tool registry.
2. `ctx.register_command(...)` — `/private` and `/endprivate`.
3. `ctx.register_hook(name, callback)` — Solomon attaches to `pre_llm_call`, `post_llm_call`, `pre_tool_call`, `post_tool_call`, `on_session_start`, `on_session_end`, and (optionally) `pre_gateway_dispatch`.
4. `ctx.get_config(key, default)` — Solomon reads tenant config from Hermes.

If Hermes ever changes any of these, only `adapter.py` and `tests/test_adapter.py` need updates.

## Storage layout

```
Postgres + pgvector
├── tenants
├── raw_events            (Part 2)
├── decisions             (Part 12)
├── audit_log             (Part 10)
├── heuristics            (Part 24)
├── pending_heuristics    (Part 24)
├── skills                (Part 17)
├── predictions           (Part 13)
├── counterfactuals       (Part 13)
├── mentoring_sessions    (Part 14)
├── pending_approvals     (Part 11)
├── autonomy_state        (Part 11)
├── regret_signals        (Part 16, Job 1)
├── fragility_log         (Part 16, Job 4)
├── private_sessions      (kill-switch audit trail; never stores content)
├── cycle_log             (Part 16)
├── embeddings            (Part 17, pgvector)
├── working_memory        (Part 7)
├── open_items            (Part 7)
├── ingestion_jobs        (Part 26)
└── ingestion_documents   (Part 26)
```

Foundation YAML files (beliefs, why, principles, non-negotiables, ideal outcomes, taxonomy) live in `~/.hermes/solomon/foundation/` and `~/.hermes/solomon/taxonomy/`. They are written by the onboarding session runner and committed to the tenant's GitHub repo (Phase 2 — auto-commit).

## What lives outside the brain

- The Hermes gateway (channel adapters: Telegram, Slack, Email, SMS, etc.) — Solomon attaches to `pre_gateway_dispatch` to turn every inbound message into a RawEvent.
- The Hermes tool registry — Solomon registers its tools alongside Hermes's, no fork required.
- The Hermes cron scheduler — Solomon installs one job for the nightly sleep cycle and one for hourly prediction checks.

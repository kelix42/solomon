# Changelog

## 0.1.0 — Phase 1 baseline

First public release. Phase 1 of the design doc is operational; Phase 2–5 features are scaffolded with TODOs where deeper work is pending.

### Built

- One-line installer (`install.sh`) detects or installs Hermes, pip-installs `solomon-brain`, runs `solomon init`.
- Pip-installable Hermes plugin via the `hermes_agent.plugins` entry point. No fork of Hermes required.
- The **translator** (`solomon/adapter.py`) — the only file that knows Hermes internals. Tested against the documented `PluginContext` contract (`register_tool`, `register_command`, `register_hook`, `get_config`).
- The **Conductor** (`solomon/conductor.py`) wraps every Hermes turn. Pre-LLM: classify, salience, non-negotiable check, working memory, multi-lane retrieval, System 1 prediction, System 2 reasoning, surprise score, audit gate. Post-LLM: log decision, schedule predictions and counterfactuals, update working memory.
- The **`/private` kill switch**. Toggle on with `/private`, off with `/private off` or `/endprivate`. Skips all logging, scoring, audit, and embedding for that conversation. Non-negotiable check still runs.
- Postgres + pgvector schema (`solomon/storage/schema.sql`) with all design-doc tables: decisions, heuristics, predictions, counterfactuals, audit_log, autonomy_state, raw_events, embeddings, working_memory, regret_signals, fragility_log, private_sessions, cycle_log, ingestion_jobs, ingestion_documents.
- Salience scorer (Sonnet, 4 dims with configurable weights).
- Classifier (scope + domain + decision type against the tenant's taxonomy).
- Non-negotiable checker (keyword / regex / LLM rule types from `non_negotiables.yaml`).
- Working memory (Postgres TTL backend; Redis swap planned for Phase 5).
- Multi-lane retrieval (recency, entity, foundation in full; semantic and pressure scaffolded with TODOs).
- System 1 (Sonnet, rules only), System 2 (Opus, full context).
- Divergence score (Jaccard for now; embedding-based later).
- Audit gate (Opus, JSON verdict).
- Autonomy ladder (4 levels per scope; 30-day observe-only default).
- Decision log + audit log persistence.
- Predictions + counterfactuals storage (LLM-generated).
- Sleep cycle runner + 8 jobs (Jobs 2, 6, 7, 8 fully implemented; Jobs 1, 3, 4, 5 scaffolded with TODOs for the LLM-driven steps).
- Onboarding curriculum (six sessions with full questions), session runner.
- Ingestion queueing + DB tables.
- Solomon CLI: `init`, `doctor`, `onboard`, `sleep`, `uninstall`.
- 18 tests covering the adapter, raw event capture, and divergence score.
- GitHub Actions CI (3.10 / 3.11 / 3.12).

### Roadmap

See `docs/PHASES.md`. Next priorities:

- Embeddings (semantic lane + ingestion pipeline)
- LLM-driven outcome matching for Job 1 hindsight
- Full mutation library for Job 4 stress test
- Owner review UI (FastAPI app)
- Ingestion pipeline: classify → chunk → embed → extract → mine
- Industry modules

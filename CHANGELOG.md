# Changelog

## 0.2.0 — Ingestion pipeline complete

Onboarding is now actually onboarding. The six-session interview was already there; this release adds the historical-document ingestion that the design doc requires as the other half of onboarding.

### Added — ingestion (Part 26)

- **Embedder** (`solomon/ingestion/embedder.py`). Default: local `sentence-transformers/all-MiniLM-L6-v2` (384-dim, CPU, no API calls). Opt-in: OpenAI `text-embedding-3-small` (1536-dim) via `SOLOMON_EMBEDDING_PROVIDER=openai`.
- **Sensitivity filter** (`solomon/ingestion/sensitivity_filter.py`). Regex PII redaction before anything else touches the text: SSN, Canadian SIN, credit cards, phone numbers, passports, email addresses. Plus owner-flagged "skip-this-document" mode.
- **Document classifier** (`solomon/ingestion/classifier.py`). One LLM call per document — type, time period, participants, domain, salience estimate. Falls back to filename heuristics when the LLM isn't configured.
- **Type-specific chunker** (`solomon/ingestion/chunker.py`). Email threads split by message, transcripts by speaker turn (short turns merged), contracts and SOPs by heading, generic by paragraph with overlap.
- **Decision extractor** (`solomon/ingestion/extractor.py`). Pulls situation / options / decision / reasoning / outcome / decision-maker / timestamp from each chunk that looks decision-rich. Cheap keyword pre-filter saves LLM calls. Confidence floor of 0.3 to drop low-quality extractions.
- **Heuristic miner** (`solomon/ingestion/heuristic_miner.py`). After all documents are processed, one cross-document pass finds repeated patterns and proposes heuristics into `pending_heuristics` for owner review. Decisions made by non-owners are extracted but excluded from mining.
- **Cross-referencer** (`solomon/ingestion/cross_referencer.py`). Phase 1: regex-only. Subject continuation, thread-id matching, filename-similarity ≥80%.
- **Budget tracker** (`solomon/ingestion/budget_tracker.py`). Per-tenant monthly token cap (default 1M, env-overridable). Pause + skip deep stages when budget is exhausted.
- **Review queue** (`solomon/ingestion/review_queue.py`). DB-backed approve/reject/defer/promote for pending heuristics.
- **Upload handler** (`solomon/ingestion/upload_handler.py`). The orchestrator. Runs all seven stages per document, then the two cross-document passes.
- **CLI**: `solomon ingest PATH...`, `solomon ingestion review`, `solomon ingestion list`.
- **Onboarding flow updated**: after Session 6, the runner prints a clear "you're not done yet — now ingest your historical material" message with the exact next commands.

### Changed

- `embeddings.vector` column went from `vector(1536)` to `vector(384)` to match the local default. Users opting into OpenAI embeddings will need to migrate that column.
- `pyproject.toml`: new optional extra `local-embeddings` pulls `sentence-transformers`. `install.sh` installs it by default.

### Tests

17 new tests covering the sensitivity filter, chunker, and budget tracker. **35/35 tests pass.**

---

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

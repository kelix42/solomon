# Solomon Build Phases

Solomon builds in five phases. Each phase ships something the user can run end-to-end.

## Phase 0 — Onboarding & Ingestion (build last, used first)

These are the entry point for any new tenant. We build them after Phases 1–3 are stable so that when a tenant onboards, the live brain works on day one.

**In repo today (Phase 1 baseline):**
- ✅ Industry & business model picker scaffold (`solomon/onboarding/industry_selector.py` — placeholder)
- ✅ Onboarding curriculum YAML (`solomon/onboarding/curriculum/sessions.yaml`)
- ✅ Session runner (`solomon/onboarding/session_runner.py`)
- ✅ Foundation YAML writer (in session_runner)
- ✅ Ingestion queue + DB tables (`solomon/ingestion/__init__.py`, `solomon/storage/schema.sql`)
- 🚧 Seed heuristic extraction from sessions
- 🚧 Industry modules (real_estate, construction, legal, professional_services)
- 🚧 Ingestion pipeline: classify, chunk, embed, extract, mine, cross-reference, sensitivity filter, owner review
- 🚧 Owner review UI

## Phase 1 — The basics (THIS IS WHAT IS BUILT)

✅ Project skeleton (pyproject.toml, plugin.yaml, install.sh)
✅ The translator (`solomon/adapter.py`)
✅ RawEvent + gateway message conversion
✅ Conductor wired into Hermes lifecycle hooks
✅ Salience scorer
✅ Classifier
✅ Non-negotiable checker
✅ Audit gate
✅ Decision logging to Postgres
✅ /private slash command
✅ Storage schema + connection pool

## Phase 2 — Brain features (in flight)

✅ Predict before reason (System 1 + System 2)
✅ Surprise score / divergence
✅ Working memory (Postgres TTL backend)
✅ Multi-lane retrieval (3 of 5 lanes implemented)
🚧 Semantic lane (needs embedder)
🚧 Pressure lane (needs urgency feature extractor)

## Phase 3 — Sleep and prediction

✅ Sleep cycle runner
✅ Job 1 — Hindsight (scaffold; outcome matcher TODO)
✅ Job 2 — Rule archival (full)
✅ Job 3 — Surprise replay (scaffold; LLM proposal TODO)
✅ Job 4 — Stress test (scaffold; mutation library TODO)
✅ Job 5 — Conflict detection (scaffold)
✅ Job 6 — Working memory cleanup (full)
✅ Job 7 — Autonomy re-evaluation (full)
✅ Job 8 — Mentoring scheduler (full)
✅ Predictions table + checkpoint scheduler scaffold
✅ Counterfactual generation
🚧 Counterfactual evaluation against actual outcomes

## Phase 4 — Smarter learning

✅ Stress test job scaffolded
✅ Conflict detection job scaffolded
🚧 Skills vs facts split
✅ Mentoring scheduler with gap analysis

## Phase 5 — Polish

✅ Autonomy ladder logic with auto-demote (Job 7)
🚧 Per-tenant isolation hardening (row-level security)
🚧 Failure modes and recovery
🚧 Observability (Langfuse for AI tracing, Sentry for errors)

## What "Phase 1 done" looks like in practice

A user runs `bash install.sh`. The script:
1. Installs Hermes if missing.
2. pip-installs solomon-brain.
3. Starts a local Postgres+pgvector container.
4. Runs `solomon init`, which migrates the schema and enables the plugin in Hermes config.
5. Optionally walks the user through `solomon onboard session_1`.

After that, the user runs `hermes` and every conversation flows through Solomon. The conductor scores salience, classifies, retrieves recent context, runs S1 and S2, computes surprise, calls the audit gate, and logs the decision. The autonomy ladder starts at `watch` for all scopes for 30 days. The sleep cycle runs every night at 02:00 and writes a `cycle_log` row.

If the user runs `/private`, that conversation is silent — no scoring, no logging, no audit gate (the non-negotiable check still runs).

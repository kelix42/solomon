# Solomon — Build State Snapshot (2026-05-22)

This file is the source of truth for "what state is the build in right now" so we don't lose context between sessions. Update at the end of every working session.

## Last decision

**Best-of-both build is the plan.** We took the architecture from the Drive version of Solomon (already in `/root/projects/solomon-from-drive/`, downloaded for reference, not modified), combined with the Hermes-plugin shell we built earlier in this repo.

**Storage:** SQLite WAL by default (single file at `~/.hermes/solomon/solomon.db`). Postgres opt-in via `SOLOMON_DB_URL=postgresql://...`. The storage layer at `solomon/storage/pool.py` hides the difference behind a single API.

**Embeddings:** local `sentence-transformers/all-MiniLM-L6-v2` by default (384-dim, free, no API calls). OpenAI `text-embedding-3-small` opt-in.

**Multimodal fallback** (scanned PDFs / images during corpus ingestion): the LONE remote dependency, documented as such in the corpus README. Everything else local.

## What's done — tested, working, committed

- ✅ **Storage layer** with SQLite WAL default + Postgres opt-in (`solomon/storage/pool.py`, ~250 lines)
- ✅ **Unified schema** at `solomon/storage/schema.sql` — 33 tables covering events, captured_items, vocabulary, coverage, clarification_queue, sessions, ingested_files, proposed_rules, mentoring_queue, wiki_vectors, plaud_state, biometrics, scope_autonomy (L0–L4), embeddings (with source_table namespace discriminator), and everything else from both designs
- ✅ Postgres overlay at `solomon/storage/schema_postgres.sql` for the pgvector + JSONB upgrades
- ✅ **Foundation YAMLs** ported from Drive verbatim: `foundation/00-industry.yaml` through `06-scopes.yaml` + JSON schemas under `foundation/_schemas/`
- ✅ **References** ported from Drive: `references/eliza-listening.md` (the seven mirroring rules), `interview-architecture.md`, `autonomy-spectrum.md`, `orchestrator-pipeline.md`, `sleep-cycle-jobs.md`, `retrieval-5-lane.md`, `portability.md`
- ✅ **SOUL.md** (the Solomon voice + decision philosophy + ELIZA-listening pin)
- ✅ **Three deep-dive reports** at `docs/REPORT-INTERVIEW.md`, `REPORT-CORPUS.md`, `REPORT-PIPELINE.md` — these are the integration plans
- ✅ **Sensitivity filter upgraded** to spaCy NER + regex + allowlist (graceful fallback to regex-only when spaCy isn't installed). 6/6 existing tests still pass.
- ✅ **Divergence formula** upgraded to `0.6·jaccard + 0.4·(1 − length_ratio)`. 5 tests pass.
- ✅ **Tests: 36/36 passing.**

## What's partially built — files exist but need wiring + verification

**Interview engine** (Subagent 1 got most of the way through but didn't finish wiring):
- ✅ All 7 probe library YAMLs at `solomon/onboarding/probe_library/`: industry, belief_system, why, principles, ideal_outcomes, non_negotiables, scopes, _generic
- ✅ `engine.py`, `extraction.py`, `vocabulary.py`, `coverage.py`, `contradiction.py`, `redact.py` files exist
- ⏳ `session_runner.py` — needs rewrite to use the new engine modules (currently still the old plain-Q&A version)
- ⏳ Verify the 5-stage flow (setup → discovery → required-fields → closing checkpoint → YAML render) actually runs end-to-end
- ⏳ Verify ulid + spacy deps land in `pyproject.toml`
- ⏳ Add tests: `test_interview_engine.py`, `test_coverage.py`

**Corpus pipeline** (Subagent 2 got the scaffolding done):
- ✅ Directory structure: `corpus/{inbox,raw,wiki}/<category>/` with `.gitkeep`
- ✅ `corpus/schema.md` (owner-editable config) + `corpus/README.md`
- ✅ `solomon/corpus/__init__.py` (with NAMESPACE_WEIGHTS constant)
- ✅ `solomon/corpus/schema_config.py`, `route.py`
- ❌ `extract.py` (hybrid file-type extraction — pypdf/docx/pptx/xlsx/html/eml/csv/json + Sonnet multimodal fallback)
- ❌ `chunk.py` (delegate to our type-aware chunker + Drive's sliding-window fallback)
- ❌ `embed.py` (wrapper around `ingestion/embedder.py` adding source_table)
- ❌ `llm.py`, `llm_passes.py`, `prompts.py` (Karpathy two-pass pattern)
- ❌ `wiki.py` (section-hash diff, swap Pinecone for embeddings table)
- ❌ `rules.py` (proposed_rules → mentoring_queue mining)
- ❌ `manifest.py` (SHA256 dedup via ingested_files)
- ❌ `lint.py`, `forget.py`, `ingest.py` (orchestrator)
- ❌ `solomon/workers/corpus_inbox_watcher/` (watchdog inbox watcher)
- ❌ `solomon/workers/plaud_ingest/` (IMAP IDLE for Plaud voice recordings)

**Decision pipeline** (Subagent 3 got 5 of 10 stages done):
- ✅ `solomon/pipeline/__init__.py`, `_helpers.py` (get_event, update_event, stage_timer)
- ✅ `stage_capture.py`, `stage_salience.py`, `stage_classification.py`, `stage_hard_rule.py` (JSON-logic against foundation/05-non-negotiables.yaml), `stage_retrieval.py`
- ❌ `stage_system1.py`, `stage_system2.py` (with new divergence inline), `stage_audit.py`, `stage_owner_state.py` (biometrics → ceiling), `stage_action.py` (L0–L4 routing)
- ❌ `runner.py` (the 10-stage walker with halt-on-skipped + halt-on-blocked)
- ❌ `solomon/non_negotiables/check.py` rewrite — shim over `stage_hard_rule.evaluate_rules`
- ❌ `solomon/conductor.py` modification — read `SOLOMON_PIPELINE_MODE`, insert events row, call `pipeline.runner.run`, populate TurnContext
- ❌ `solomon/autonomy/ladder.py` rename to L0–L4 + `owner_state_ceiling()` + `effective_for()`
- ❌ `solomon/storage/decisions.py` rewrite to use new pool API + mirror events row
- ❌ 4 new sleep jobs: `job_9_corpus_lint.py`, `job_10_corpus_backup.py`, `job_11_embed_pending.py`, `job_12_yaml_reconcile.py`
- ❌ Append new jobs to `solomon/sleep/runner.py::JOB_ORDER`

**CLI + Install** (deferred until subsystems are done):
- ❌ `solomon corpus watch / ingest / stats` CLI subcommands
- ❌ Rewrite `install.sh` to walk the user end-to-end: install Hermes if missing → pip install solomon-brain → `solomon init` → onboarding session 0 → session 1 → … → session 6 → ingestion prompt → review queue → "you're in observe-only mode now"
- ❌ Add to `pyproject.toml`: `ulid-py>=1.1`, `spacy>=3.7`, `pypdf>=4.0`, `python-docx>=1.1`, `python-pptx>=0.6`, `openpyxl>=3.1`, `beautifulsoup4>=4.12`, `watchdog>=4.0`, `imapclient>=3.0`, `json-logic-qubit>=0.9`. Optional extras: `[redaction-spacy]`, `[local-embeddings]`.

## Recommended next-session priority order

1. **Finish the interview engine** (rewrite session_runner.py to use the 5-stage flow, add tests, verify ulid + spacy deps). This is the user-facing piece. Without it nothing else matters.
2. **Finish the pipeline** (stages 6–10, runner.py, non_negotiables shim, conductor wire-up, autonomy L0–L4). The brain's live loop.
3. **Finish the corpus** (extract → chunk → embed → llm_passes → wiki → rules → ingest, then the two workers). Required for onboarding to be complete per the design.
4. **4 new sleep jobs.**
5. **Rewrite install.sh** to walk the user through the full onboarding flow on first install.
6. **Push to GitHub.**

## Pinned reading order for next session

Before writing any code, re-read:
1. `BUILD-STATE.md` (this file)
2. `docs/REPORT-INTERVIEW.md` section 4 (the integration plan)
3. `docs/REPORT-PIPELINE.md` section 4
4. `docs/REPORT-CORPUS.md` section 4
5. `references/eliza-listening.md` (the seven mirroring rules — pin to every interview-phase LLM call)

## Files modified or added in the last session

```
M pyproject.toml                       # json-logic-qubit, ulid-py, sentence-transformers
M solomon/ingestion/sensitivity_filter.py  # spaCy NER + regex
M solomon/reasoning/divergence.py     # new formula: 0.6·jaccard + 0.4·(1−length_ratio)
M solomon/storage/__init__.py          # re-export the new API
M solomon/storage/pool.py              # SQLite WAL + Postgres dual-backend
M solomon/storage/schema.sql           # 33 tables, unified
M tests/test_divergence.py            # 5 new tests for new formula

+ SOUL.md
+ corpus/                              # owner-editable config + 3-layer folder structure
+ docs/REPORT-CORPUS.md
+ docs/REPORT-INTERVIEW.md
+ docs/REPORT-PIPELINE.md
+ foundation/                          # all 7 YAMLs + JSON schemas
+ references/                          # 7 design docs from Drive
+ solomon/corpus/__init__.py, route.py, schema_config.py
+ solomon/onboarding/interview/{__init__,contradiction,coverage,engine,extraction,redact,vocabulary}.py
+ solomon/onboarding/probe_library/{README.md, _generic, belief_system, ideal_outcomes, industry, non_negotiables, principles, scopes, why}.yaml
+ solomon/pipeline/{__init__, _helpers, stage_capture, stage_classification, stage_hard_rule, stage_retrieval, stage_salience}.py
+ solomon/storage/schema_postgres.sql
+ BUILD-STATE.md                       # this file
```

**Tests:** 36/36 passing on SQLite. Run `pytest tests/ -v` to verify.

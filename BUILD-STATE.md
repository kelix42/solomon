# Solomon — Build State Snapshot (2026-05-23 morning)

This file is the source of truth for "what state is the build in right now" so we don't lose context between sessions. Update at the end of every working session.

## Last decision

**Best-of-both build is the plan.** We took the architecture from the Drive version of Solomon (already in `/root/projects/solomon-from-drive/`, downloaded for reference, not modified), combined with the Hermes-plugin shell we built earlier in this repo.

**Storage:** SQLite WAL by default (single file at `~/.hermes/solomon/solomon.db`). Postgres opt-in via `SOLOMON_DB_URL=postgresql://...`. The storage layer at `solomon/storage/pool.py` hides the difference behind a single API.

**Embeddings:** local `sentence-transformers/all-MiniLM-L6-v2` by default (384-dim, free, no API calls). OpenAI `text-embedding-3-small` opt-in.

**Multimodal fallback** (scanned PDFs / images during corpus ingestion): the LONE remote dependency, documented as such in the corpus README. Stubbed behind `SOLOMON_ALLOW_VISION_API`; raises `UnsupportedFileType` until wired. Everything else local.

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
- ✅ **Interview engine — DONE.** Session runner rewritten to the 5-stage flow (Setup → Discovery → Required-fields → Closing checkpoint → Foundation YAML render). All 7 probe libraries wired in. Stage D intent classifier (confirm/correct/add/keep_talking/abandon) with deterministic short-circuits for the common words. Resume-on-Ctrl-C works because the `sessions` row stays `open` until Stage E succeeds. 19 new interview/coverage tests + 3 session-runner integration tests with stubbed LLM and scripted stdin.
- ✅ **Corpus pipeline — DONE (2026-05-23).** All of items 1–13 from this session's build plan are green:
  - `solomon/corpus/extract.py` — per-format dispatch over pypdf/python-docx/python-pptx/openpyxl/beautifulsoup4 + stdlib email + csv + json + multimodal-vision stub. ExtractedDoc dataclass with text, page_breaks, metadata. (19 tests)
  - `solomon/corpus/manifest.py` — SHA256-keyed ingested_files manifest via the pool API. Idempotent on SHA; list_by_status() + stats() helpers. (12 tests)
  - `solomon/corpus/chunk.py` — type-aware delegation to `solomon.ingestion.chunker` + Drive-style sliding-window fallback. Chunk dataclass with char_offsets + source_section. (9 tests)
  - `solomon/corpus/embed.py` — wrapper over `solomon.ingestion.embedder.embed_batch` with the `source_table` discriminator (`corpus_raw` / `corpus_wiki` / `captured_items` / `decisions`). Float32-packed BLOB on SQLite. (12 tests)
  - `solomon/corpus/prompts.py` + `llm.py` + `llm_passes.py` — Karpathy two-pass. Pass 1 returns the JSON envelope; Pass 2 merges per-page markdown and triggers the section-hash upsert. (17 tests)
  - `solomon/corpus/wiki.py` — section-hash diff over the embeddings table. Idempotent re-writes; orphan-vector cleanup; `remove_page()` for forget cascade. (16 tests)
  - `solomon/corpus/rules.py` — THE CRITICAL FILE. Pass 1's proposed_rules become `proposed_rules` rows + paired `mentoring_queue` rows (source='corpus_rule_proposal', priority=4). Dedup on (tenant_id, source_path, verbatim_excerpt). delete_for_source() for forget cascade. (16 tests)
  - `solomon/corpus/lint.py` — health checks: orphan raw embeddings, broken wiki pages, orphan wiki embeddings, forgotten files with lingering rows, orphan queued proposed_rules. LintFinding dataclass + summary(). (11 tests)
  - `solomon/corpus/forget.py` — owner-initiated deletion cascade: file → embeddings → proposed_rules → mentoring_queue → mark forgotten. (7 tests)
  - `solomon/corpus/ingest.py` — orchestrator. Full walk through extract → dedup → route → chunk → embed → Pass 1/2 → rules → mark success. Failure modes mark 'partial' / 'failed' cleanly. (7 tests)
  - `solomon/workers/corpus_inbox_watcher/` — watchdog-based inbox watcher with 30s debounce, 3s file-stable check, catch-up scan, polling fallback. (17 tests)
  - `solomon/workers/plaud_ingest/` — STUB. Persistent state + config + save_attachment all tested; main() refuses to start without IMAP creds + warns the listener thread is deferred. (11 tests)
  - `solomon corpus ingest|watch|stats|forget|lint` CLI subcommands wired in `solomon/cli.py`. (9 tests)
- ✅ **Tests: 229/229 passing** (was 66; +163 net new from this session, all corpus).

## What's partially built — files exist but need wiring + verification

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

**Review CLI + Install** (deferred to next session):
- ❌ `solomon/mentoring/review.py` + `solomon mentoring review` CLI subcommand. Reads mentoring_queue ORDER BY priority, status='queued'. Owner picks approve / reject / edit. Approve = INSERT into heuristics, mark queue row resolved.
- ❌ Rewrite `install.sh` to walk the user end-to-end: install Hermes if missing → pip install solomon-brain → `solomon init` → onboarding session 0 → … → session 6 → corpus ingest prompt → review queue → "you're in observe-only mode now"

## Recommended next-session priority order

1. **Review CLI + install.sh** (items 14 + 15 from this session's build plan). Now that the corpus pipeline + mentoring_queue exists, the review CLI closes the loop end-to-end. ~200 lines.
2. **Finish the decision pipeline** (stages 6–10, runner.py, non_negotiables shim, conductor wire-up, autonomy L0–L4). The brain's live loop.
3. **4 new sleep jobs** — `job_9_corpus_lint` would call `solomon.corpus.lint.run_lint()` (already implemented); the rest are smaller.
4. **Push to GitHub.**

## Pinned reading order for next session

Before writing any code, re-read:
1. `BUILD-STATE.md` (this file)
2. `docs/REPORT-PIPELINE.md` section 4 (the integration plan)
3. `references/eliza-listening.md` (the seven mirroring rules — pin to every interview-phase LLM call)

## Files modified or added in the last session (2026-05-23 corpus pipeline)

```
+ solomon/corpus/extract.py
+ solomon/corpus/manifest.py
+ solomon/corpus/chunk.py
+ solomon/corpus/embed.py
+ solomon/corpus/prompts.py
+ solomon/corpus/llm.py
+ solomon/corpus/llm_passes.py
+ solomon/corpus/wiki.py
+ solomon/corpus/rules.py             # THE CRITICAL FILE
+ solomon/corpus/lint.py
+ solomon/corpus/forget.py
+ solomon/corpus/ingest.py
+ solomon/workers/__init__.py
+ solomon/workers/corpus_inbox_watcher/__init__.py
+ solomon/workers/corpus_inbox_watcher/__main__.py
+ solomon/workers/plaud_ingest/__init__.py
+ solomon/workers/plaud_ingest/__main__.py

+ tests/test_corpus_extract.py
+ tests/test_corpus_manifest.py
+ tests/test_corpus_chunk.py
+ tests/test_corpus_embed.py
+ tests/test_corpus_llm_passes.py
+ tests/test_corpus_wiki.py
+ tests/test_corpus_rules.py
+ tests/test_corpus_lint.py
+ tests/test_corpus_forget.py
+ tests/test_corpus_ingest.py
+ tests/test_corpus_inbox_watcher.py
+ tests/test_plaud_ingest.py
+ tests/test_corpus_cli.py

M solomon/corpus/__init__.py          # tolerate incremental builds
M solomon/cli.py                      # add `solomon corpus ...` subcommands
M pyproject.toml                      # add pypdf/python-docx/python-pptx/
                                       # openpyxl/beautifulsoup4/watchdog/
                                       # imapclient as required deps
M BUILD-STATE.md                      # this file
```

**Tests:** 229/229 passing on SQLite. Run `pytest tests/ -v` to verify.

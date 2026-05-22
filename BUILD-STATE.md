# Solomon — Build State Snapshot (2026-05-24 morning)

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
- ✅ **References** ported from Drive: `references/eliza-listening.md`, `interview-architecture.md`, `autonomy-spectrum.md`, `orchestrator-pipeline.md`, `sleep-cycle-jobs.md`, `retrieval-5-lane.md`, `portability.md`
- ✅ **SOUL.md** (the Solomon voice + decision philosophy + ELIZA-listening pin)
- ✅ **Three deep-dive reports** at `docs/REPORT-INTERVIEW.md`, `REPORT-CORPUS.md`, `REPORT-PIPELINE.md` — these are the integration plans
- ✅ **Sensitivity filter upgraded** to spaCy NER + regex + allowlist (graceful fallback to regex-only when spaCy isn't installed). 6/6 existing tests still pass.
- ✅ **Divergence formula** upgraded to `0.6·jaccard + 0.4·(1 − length_ratio)`. 5 tests pass.
- ✅ **Interview engine — DONE.** Session runner rewritten to the 5-stage flow (Setup → Discovery → Required-fields → Closing checkpoint → Foundation YAML render). All 7 probe libraries wired in. Stage D intent classifier (confirm/correct/add/keep_talking/abandon) with deterministic short-circuits for the common words. Resume-on-Ctrl-C works because the `sessions` row stays `open` until Stage E succeeds. 19 new interview/coverage tests + 3 session-runner integration tests with stubbed LLM and scripted stdin.
- ✅ **Corpus pipeline — DONE (2026-05-23).** All of items 1–13 from the corpus build plan green: extract / manifest / chunk / embed / Karpathy two-pass LLM / wiki / rules / lint / forget / ingest orchestrator + `corpus_inbox_watcher` worker + `plaud_ingest` stub + `solomon corpus ingest|watch|stats|forget|lint` CLI. (~163 tests).
- ✅ **Review CLI — DONE (2026-05-24).** `solomon/mentoring/review.py` + `solomon mentoring review` CLI subcommand. Reads `mentoring_queue ORDER BY priority ASC, surfaced_at ASC` where status='queued'. For each `corpus_rule_proposal` source, loads the matching `proposed_rules` row and prompts: `[a]pprove / [r]eject / [e]dit / [s]kip / [q]uit`.
  - approve → INSERT into `heuristics` (scope='business', source='corpus_review', confidence=0.5, status='active', provenance JSON carrying proposed_rule_id + source_path + verbatim_excerpt + confidence_hint); `proposed_rules.status='approved'`; `mentoring_queue.status='resolved'`.
  - reject → `proposed_rules.status='rejected'`; `mentoring_queue.status='dismissed'`. Nothing inserted.
  - edit → prompt for new statement (blank = keep current), then approve with the edited text. Counted as `edited`, not `approved`.
  - skip → both rows left as-is.
  - quit → remaining items stay queued; clean exit.
  - Non-`corpus_rule_proposal` sources (contradiction / drift / promotion_ready / demotion_alert) are noted with a one-line skip message — they get their own review flows later.
  - 11 unit tests + 5 CLI tests (both via the `solomon_db` fixture; CLI tests monkeypatch `builtins.input`).
- ✅ **install.sh — DONE (2026-05-24).** Rewritten end-to-end: detect-or-install Hermes → pip install solomon-brain → `solomon init` → restart Hermes gateway → print onboarding next-steps (interview sessions 0-6, corpus drop, mentoring review, autonomy L0). `set -euo pipefail`, idempotent (each step short-circuits with "✓ already installed"), `--dry-run` flag prints every command without executing, `--help` prints the header. `bash -n install.sh` syntax-clean.
- ✅ **Tests: 245/245 passing** (was 229 at session start; +16 from review CLI).

## What's partially built — files exist but need wiring + verification

**Decision pipeline** (Subagent 3 got 5 of 10 stages done — this is the next major chunk):
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

## Recommended next-session priority order

1. **Finish the decision pipeline.** Stages 6–10 + `runner.py` (the 10-stage walker) + `non_negotiables/check.py` shim + `conductor.py` wire-up + `autonomy/ladder.py` L0–L4 rename. This is the brain's live loop — once it runs, every Hermes turn flows through it. Reference: `docs/REPORT-PIPELINE.md` §4.
2. **4 new sleep jobs** — `job_9_corpus_lint` is mechanical (it just calls `solomon.corpus.lint.run_lint()` which is already done); `job_10_corpus_backup`, `job_11_embed_pending`, `job_12_yaml_reconcile` are also small. Then append to `JOB_ORDER`.
3. **`solomon/storage/decisions.py` rewrite** to use the new pool API (`?` placeholders, `get_conn`/`cursor`/`execute`). Currently uses `%s` directly and crashes on SQLite — pipeline tests will hit this immediately.
4. **Push to GitHub.**

## Pinned reading order for next session

Before writing any code, re-read:
1. `BUILD-STATE.md` (this file)
2. `docs/REPORT-PIPELINE.md` section 4 (the integration plan)
3. `references/eliza-listening.md` (the seven mirroring rules — pin to every interview-phase LLM call)

## Files modified or added in the last session (2026-05-24 review CLI + install.sh)

```
+ solomon/mentoring/review.py        # ~370 lines — the review loop
+ tests/test_mentoring_review.py     # 11 unit tests
+ tests/test_mentoring_cli.py        # 5 CLI tests

M solomon/cli.py                     # add `solomon mentoring review` dispatch
M install.sh                         # full rewrite: idempotent, --dry-run,
                                     # detect-or-install Hermes, prints
                                     # onboarding next-steps
M BUILD-STATE.md                     # this file
```

**Tests:** 245/245 passing on SQLite. Run `pytest tests/ -v` to verify.

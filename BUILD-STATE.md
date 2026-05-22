# Solomon — Build State Snapshot (2026-05-25 morning)

This file is the source of truth for "what state is the build in right now" so we don't lose context between sessions. Update at the end of every working session.

## Last decision

**Best-of-both build is the plan.** We took the architecture from the Drive version of Solomon (already in `/root/projects/solomon-from-drive/`, downloaded for reference, not modified), combined with the Hermes-plugin shell we built earlier in this repo.

**Storage:** SQLite WAL by default (single file at `~/.hermes/solomon/solomon.db`). Postgres opt-in via `SOLOMON_DB_URL=postgresql://...`. The storage layer at `solomon/storage/pool.py` hides the difference behind a single API.

**Embeddings:** local `sentence-transformers/all-MiniLM-L6-v2` by default (384-dim, free, no API calls). OpenAI `text-embedding-3-small` opt-in.

**Multimodal fallback** (scanned PDFs / images during corpus ingestion): the LONE remote dependency, documented as such in the corpus README. Stubbed behind `SOLOMON_ALLOW_VISION_API`; raises `UnsupportedFileType` until wired. Everything else local.

## What's done — tested, working, committed

- ✅ **Storage layer** with SQLite WAL default + Postgres opt-in (`solomon/storage/pool.py`, ~330 lines incl. idempotent ADD COLUMN migrations)
- ✅ **Unified schema** at `solomon/storage/schema.sql` — 33 tables covering events, captured_items, vocabulary, coverage, clarification_queue, sessions, ingested_files, proposed_rules, mentoring_queue, wiki_vectors, plaud_state, biometrics, scope_autonomy (L0–L4), embeddings (with source_table namespace discriminator), and everything else from both designs. `events` now also has `owner_state_ceiling` and `effective_autonomy` (Session A schema bump).
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
  - approve → INSERT into `heuristics`; reject → drops the proposal; edit → re-prompts then approves; skip → leaves both rows; quit → clean exit.
  - 11 unit tests + 5 CLI tests (both via the `solomon_db` fixture; CLI tests monkeypatch `builtins.input`).
- ✅ **install.sh — DONE (2026-05-24).** Rewritten end-to-end: detect-or-install Hermes → pip install solomon-brain → `solomon init` → restart Hermes gateway → print onboarding next-steps. `set -euo pipefail`, idempotent, `--dry-run` flag, `--help`. `bash -n install.sh` syntax-clean.

### Session A — Decision pipeline stages 6–10 + runner + plumbing (2026-05-25)

- ✅ **`solomon/storage/decisions.py` — REWRITE.** Now uses the portable pool API (`get_conn` / `cursor` / `execute` / `jsonify` / `parse_json`) with `?` placeholders throughout. Crashes on SQLite are gone. New public function `mirror_event_to_decision(event_id) -> decision_id` copies a completed events row into a `decisions` row + writes an `audit_log` row when there's a verdict. `DecisionLog.log(turn)` and `get_or_create_tenant_id()` keep the same signatures so the conductor doesn't have to change. Helper `reset_tenant_cache()` for tests.
- ✅ **`solomon/autonomy/ladder.py` — REWRITE.** Adds the canonical L0–L4 scheme: `LEVEL_NAMES = {0:"manual", 1:"suggested", 2:"drafted", 3:"supervised", 4:"autonomous"}`, plus `owner_state_ceiling(state) -> int` (green→4, yellow→2, red→1, unknown→4), `effective_for(scope, owner_state) -> int`, and `scope_level(tenant_id, scope) -> int` reading the new `scope_autonomy` table. The legacy `AUTONOMY_LEVELS` 4-string tuple and `AutonomyLadder` class are kept as a deprecated compat layer so `solomon/sleep/job_7_autonomy.py` and `solomon/conductor.py` continue to import and run untouched (Session A explicitly does not touch the conductor).
- ✅ **`solomon/non_negotiables/check.py` — REWRITE.** Thin shim over `solomon.pipeline.stage_hard_rule.evaluate_rules`. Drops the legacy `keyword` / `regex` modes (subsumed by JSON-logic per `REPORT-PIPELINE.md`). The one preserved escape hatch is `check_type='llm'` (or `'fuzzy'`) — that rule routes through `solomon.reasoning.llm.get_client()` with `tier="fast"` and a strict JSON response. Public surface (`NonNegotiableChecker` + `NonNegotiableViolation`) unchanged so the conductor keeps working.
- ✅ **`solomon/pipeline/stage_system1.py` — NEW.** Self-contained tier="fast" call. Writes `system1_output` as JSON `{answer, confidence, scope}` via `jsonify`.
- ✅ **`solomon/pipeline/stage_system2.py` — NEW.** tier="deep" + inline divergence via `solomon.reasoning.divergence.divergence_score`. Writes `system2_output` JSON `{reasoning, proposed_action, confidence}` + `divergence_score` float in [0, 1].
- ✅ **`solomon/pipeline/stage_audit.py` — NEW.** Wrapper over `solomon.audit_gate.audit.AuditGate` (tier="deep"). Normalises to three verdicts: `APPROVE` / `REJECT` / `REQUEST_RETHINK`. `DOWNGRADE` collapses to `APPROVE` (Stage 10's autonomy ceiling will demote the action if needed).
- ✅ **`solomon/pipeline/stage_owner_state.py` — NEW.** Reads the most recent `biometrics` row within 24h for the tenant. Maps to ceiling via `owner_state_ceiling`. Stale (>24h) or missing → `unknown` / ceiling 4. Also derives state from `payload` (recovery_pct / sleep_hours / stress_flag) when the categorical `state` column wasn't pre-populated.
- ✅ **`solomon/pipeline/stage_action.py` — NEW.** `effective_autonomy = min(scope_level, owner_state_ceiling)`. Routes one of: `ship` (L4 + APPROVE) / `one-tap` (L2 or L3 + APPROVE) / `suggest` (L0 or L1 + APPROVE) / `escalate` (REJECT or REQUEST_RETHINK). Writes `effective_autonomy`, `action_taken`, `status='complete'`, then calls `mirror_event_to_decision` so the H2 decisions row gets created.
- ✅ **`solomon/pipeline/runner.py` — NEW.** The 10-stage walker. Halt-on-skipped (salience < 0.30 → `status='skipped'`) and halt-on-blocked (`status='blocked_by_hard_rule'`) short-circuit cleanly. Each stage call is wrapped in `_helpers.stage_timer` so per-stage elapsed_ms merges into `events.stage_timings_ms`. Returns the fresh row dict on exit. `solomon/pipeline/__init__.py` import-guards the `from .runner import run` so the package keeps loading during incremental builds.
- ✅ **Schema additions:** `events.owner_state_ceiling INTEGER`, `events.effective_autonomy INTEGER`. Fresh installs get them via the `CREATE TABLE` block; existing DBs get them via a new `_migrate_add_columns` helper in `solomon/storage/pool.py` that introspects with `PRAGMA table_info` (SQLite) / `information_schema.columns` (Postgres) and only ALTERs if absent.
- ✅ **`solomon/pipeline/_helpers.py`** — `_UPDATABLE_COLUMNS` widened to include `owner_state_ceiling` and `effective_autonomy`.
- ✅ **Tests: 315/315 passing on SQLite** (was 245). 70 new tests across `test_decisions.py`, `test_autonomy_ladder.py`, `test_non_negotiables.py`, `test_stage_system1.py`, `test_stage_system2.py`, `test_stage_audit.py`, `test_stage_owner_state.py`, `test_stage_action.py`, `test_pipeline_runner.py`. Shared LLM-stub + event-seeder helpers live in `tests/_pipeline_helpers.py`; `tests/__init__.py` was added so the helpers are importable as `tests._pipeline_helpers`.

## What's partially built — files exist but need wiring + verification

The 10-stage pipeline is complete and unit-tested. **It is NOT yet wired into the conductor** — that's Session B (see priority list below). Until Session B lands, every Hermes turn still goes through the conductor's legacy 7-step inline pre-LLM hook; the new `pipeline.runner.run` is only invoked from tests.

Remaining decision-pipeline work:

- ❌ `solomon/conductor.py` modification — read `SOLOMON_PIPELINE_MODE`, insert an events row, call `pipeline.runner.run`, populate `TurnContext` from the returned row. **Session B.** Must include kill-switch env var (`SOLOMON_PIPELINE_DISABLE=1`), try/except fall-through to legacy path, and before/after pytest baselines. See `references/critical-path-prompt-template.md` in the solomon-project skill.

Remaining sleep / repo housekeeping:

- ❌ 4 new sleep jobs: `job_9_corpus_lint.py`, `job_10_corpus_backup.py`, `job_11_embed_pending.py`, `job_12_yaml_reconcile.py`
- ❌ Append new jobs to `solomon/sleep/runner.py::JOB_ORDER`
- ❌ Push to GitHub.

## Recommended next-session priority order

1. **Session B — conductor wire-up (`solomon/conductor.py`).** Replace the in-place `_pre_llm_call` body with the four-line "insert events row → call `pipeline.runner.run(event_id)` → read row back into `TurnContext`" sequence. Add the `SOLOMON_PIPELINE_DISABLE=1` kill switch and the legacy-path fallback. Run `pytest tests/ -q` BEFORE the change to capture the 315/315 baseline, then again AFTER. Reference: `docs/REPORT-PIPELINE.md` §4.2–4.3 and the solomon-project skill's `references/critical-path-prompt-template.md`. This is the critical-path session — pair anxiety warnings with the recovery path and confidence breakdown in the prompt.
2. **Session C — 4 new sleep jobs.** `job_9_corpus_lint` is mechanical (calls `solomon.corpus.lint.run_lint()`); `job_10_corpus_backup`, `job_11_embed_pending`, `job_12_yaml_reconcile` are also small. Then append to `JOB_ORDER`.
3. **Push to GitHub.**

## Pinned reading order for next session

Before writing any code, re-read:
1. `BUILD-STATE.md` (this file)
2. `docs/REPORT-PIPELINE.md` section 4 (the integration plan)
3. `references/eliza-listening.md` (the seven mirroring rules — pin to every interview-phase LLM call)

## Files modified or added in Session A (2026-05-25, pipeline stages 6–10 + runner + plumbing)

```
+ solomon/pipeline/runner.py                  # 10-stage walker
+ solomon/pipeline/stage_system1.py           # tier=fast wrapper
+ solomon/pipeline/stage_system2.py           # tier=deep + divergence
+ solomon/pipeline/stage_audit.py             # 3-verdict normalizer
+ solomon/pipeline/stage_owner_state.py       # biometrics → ceiling
+ solomon/pipeline/stage_action.py            # min(scope, ceiling) + routing + mirror

M solomon/storage/decisions.py                # full pool-API rewrite + mirror_event_to_decision
M solomon/autonomy/ladder.py                  # L0–L4 + owner_state_ceiling + effective_for
M solomon/non_negotiables/check.py            # shim over stage_hard_rule.evaluate_rules
M solomon/storage/schema.sql                  # +owner_state_ceiling, +effective_autonomy
M solomon/storage/pool.py                     # _migrate_add_columns helper
M solomon/pipeline/__init__.py                # import-guard the runner re-export
M solomon/pipeline/_helpers.py                # widen _UPDATABLE_COLUMNS

+ tests/__init__.py                           # marker so helpers are importable
+ tests/_pipeline_helpers.py                  # shared StubLLM + seed_event + read_event
+ tests/test_decisions.py                     # 8 tests
+ tests/test_autonomy_ladder.py               # 26 tests
+ tests/test_non_negotiables.py               # 7 tests
+ tests/test_stage_system1.py                 # 2 tests
+ tests/test_stage_system2.py                 # 2 tests
+ tests/test_stage_audit.py                   # 4 tests
+ tests/test_stage_owner_state.py             # 6 tests
+ tests/test_stage_action.py                  # 10 tests
+ tests/test_pipeline_runner.py               # 5 tests

M BUILD-STATE.md                              # this file
```

**Tests:** 315/315 passing on SQLite. Run `pytest tests/ -v` to verify.

**Session A explicitly did NOT touch `solomon/conductor.py`.** That's Session B's job and the conductor is in the live per-turn hot path; bundling its modification with the mechanical stage work would have burned Opus budget on the wrong file. See the commit message for the same call-out.

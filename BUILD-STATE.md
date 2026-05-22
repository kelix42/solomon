# Solomon — Build State Snapshot (2026-05-25 evening)

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

### Session B — Conductor wire-up (2026-05-25 evening)

- ✅ **`solomon/conductor.py` — MODIFIED.** `_pre_llm_call` now drives the 10-stage pipeline via `solomon.pipeline.runner.run`, with three non-negotiable safeguards baked in. The legacy 7-step inline body is preserved verbatim as `_pre_llm_call_legacy` and runs as the fallback whenever the pipeline path is disabled or crashes. `TurnContext` gained 10 new fields (`event_id`, `classification`, `system1_output`, `system2_output`, `owner_state`, `owner_state_ceiling`, `effective_autonomy`, `action_taken`, `stage_timings_ms`, `status`) so the conductor mirrors the 14 events columns the prompt called for.
- ✅ **Kill-switch env var: `SOLOMON_PIPELINE_DISABLE`.** Truthy values (`1`, `true`, `yes`, `on`, case-insensitive) make `_pre_llm_call` skip the pipeline entirely and run the legacy body. **Recovery path for the wild:** `echo SOLOMON_PIPELINE_DISABLE=1 >> ~/.hermes/.env && hermes restart`. The kill switch is read from env on every turn (no module-load caching) so a `.env` flip takes effect on the next Hermes restart without further surgery.
- ✅ **Mode env var: `SOLOMON_PIPELINE_MODE`.** Defaults to `inline` (run the 10 stages synchronously inside `_pre_llm_call`, then populate `TurnContext` from the events row). Set to `queue` to skip in-process execution — the conductor still inserts an events row with `status='pending'` and returns; the (future) pipeline-tick worker picks it up. Unknown values fall back to `inline` with a warning. The queue-mode worker is **out of scope** for Session B; there's a TODO comment pointing at `solomon/workers/pipeline_tick/__main__.py` for the future implementer.
- ✅ **try/except wrapping the pipeline path.** Any exception during the INSERT, the `pipeline.runner.run` call, or the row read-back is caught: the events row gets `status='errored'` + `audit_reasoning="pipeline error: ..."`, and `_pre_llm_call_legacy` runs so the turn still gets a response. **A pipeline crash MUST NOT kill the Hermes turn** is enforced by both the try/except *and* a regression test that asserts the legacy path populated `turn.audit_verdict` after the runner raised.
- ✅ **Audit-verdict → system-message mapping.** Injected into the `messages` list passed to the hook (lists are mutable; Hermes sees the append). `TurnContext.system_prompt` is intentionally NOT touched because that field is overwritten elsewhere in the Hermes pipeline.
    - `status='skipped'` (low salience) → **no message** injected; continue.
    - `status='blocked_by_hard_rule'` → decline message citing the non-negotiable reason.
    - `status='complete'` + `audit_verdict='REJECT'` → decline message with audit reasoning.
    - `status='complete'` + `audit_verdict='REQUEST_RETHINK'` → rethink message with audit reasoning.
    - `status='complete'` + `audit_verdict='APPROVE'` → **no message**; continue.
    - Errored / pending / unknown → **no message** (conservative).
- ✅ **`_post_llm_call`** now calls `solomon.storage.decisions.mirror_event_to_decision(event_id)` when the turn carries an `event_id`, so the sleep-cycle and review-queue consumers see the decisions row. The call is idempotent: `mirror_event_to_decision` checks for an existing decisions row by `event_id` before inserting, so the duplicate that `stage_action`'s own mirror call would otherwise produce in inline mode is silently absorbed. In queue mode this populates the decisions row right after the gateway response (the row carries whatever the events row had at that point — usually still `pending`).
- ✅ **`solomon/storage/decisions.py`** — `mirror_event_to_decision` is now idempotent on `event_id`. Existing 8 decisions tests still pass.
- ✅ **Tests: 327/327 passing on SQLite** (315 → 327). 12 new tests in `tests/test_conductor_pipeline.py` covering: kill-switch + no events insert, inline happy path + 14-column population, low-salience skipped (no message), hard-rule block (decline message), audit REJECT (decline), audit REQUEST_RETHINK (rethink), queue mode (pending + no runner), runner-raises (errored + legacy fallback), post_llm_call mirror idempotency, plus three unit tests for the `_pipeline_disabled` / `_pipeline_mode` env-var parsers. Existing conductor surface (no tests targeting it before, but 315 tests across the rest of the codebase) all still green.

## What's partially built — files exist but need wiring + verification

The conductor wire-up is now live. The 10-stage pipeline runs on every non-private Hermes turn (unless `SOLOMON_PIPELINE_DISABLE=1`).

Remaining sleep / repo housekeeping:

- ❌ 4 new sleep jobs: `job_9_corpus_lint.py`, `job_10_corpus_backup.py`, `job_11_embed_pending.py`, `job_12_yaml_reconcile.py`
- ❌ Append new jobs to `solomon/sleep/runner.py::JOB_ORDER`
- ❌ Push to GitHub.
- ❌ (Future, not blocking) The queue-mode pipeline-tick worker at `solomon/workers/pipeline_tick/__main__.py`. The conductor inserts the row; until the worker exists, `SOLOMON_PIPELINE_MODE=queue` essentially fire-and-forgets to a stale row. **Default mode is `inline` so this doesn't bite us.**

## Recommended next-session priority order

1. **Session C — 4 new sleep jobs.** `job_9_corpus_lint` is mechanical (calls `solomon.corpus.lint.run_lint()`); `job_10_corpus_backup`, `job_11_embed_pending`, `job_12_yaml_reconcile` are also small. Then append to `JOB_ORDER`. After that, push to GitHub.
2. **(Stretch, post-C)** Build the pipeline-tick worker so `SOLOMON_PIPELINE_MODE=queue` becomes a real option (offloads the synchronous runner cost from the user-visible turn).

## Pipeline kill-switch — pinned recovery path

If the pipeline misbehaves in the wild (the per-turn hot path is now driving the 10 stages):

```
echo SOLOMON_PIPELINE_DISABLE=1 >> ~/.hermes/.env
hermes restart      # or: systemctl restart hermes if running as a service
```

That flips `_pre_llm_call` back to the bit-for-bit pre-Session-B body. No code change, no rollback, no uninstall. Confirm recovery by sending a Telegram message; if Hermes responds normally and `solomon.conductor` log lines stop mentioning `pipeline` at INFO level, you're back to baseline.

## Pinned reading order for next session

Before writing any code, re-read:
1. `BUILD-STATE.md` (this file)
2. `docs/REPORT-PIPELINE.md` section 4 (the integration plan)
3. `references/sleep-cycle-jobs.md` (the 12-job catalogue) for Session C
4. `references/eliza-listening.md` (the seven mirroring rules — pin to every interview-phase LLM call)

## Files modified or added in Session A (2026-05-25 morning, pipeline stages 6–10 + runner + plumbing)

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

## Files modified or added in Session B (2026-05-25 evening, conductor wire-up)

```
M solomon/conductor.py                        # _pre_llm_call drives pipeline.runner.run; legacy body preserved as _pre_llm_call_legacy; TurnContext gained 10 pipeline-column fields; _post_llm_call mirrors to decisions
M solomon/storage/decisions.py                # mirror_event_to_decision is now idempotent on event_id

+ tests/test_conductor_pipeline.py            # 12 tests: kill-switch, inline happy, low-salience, hard-rule block, audit REJECT, audit REQUEST_RETHINK, queue mode, runner crash → errored + legacy fallback, post_llm_call mirror idempotency, plus env-var parser units

M BUILD-STATE.md                              # this file
```

**Tests:** 327/327 passing on SQLite (315 baseline + 12 new in Session B). Run `pytest tests/ -q` to verify. Also verified green with `SOLOMON_PIPELINE_DISABLE=1` set (kill-switch path produces bit-for-bit pre-Session-B behaviour).

**Session B explicitly did NOT touch `solomon/pipeline/`, `solomon/corpus/`, `solomon/mentoring/`, `install.sh`, or any sleep job.** Those are either done (Session A and earlier) or scheduled for Session C.

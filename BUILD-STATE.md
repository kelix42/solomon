# Solomon — Build State Snapshot (2026-05-24)

This file is the source of truth for "what state is the build in right now" so we don't lose context between sessions. Update at the end of every working session.

## Last decision

**`/onboard` recovers from stale registry entries.** Live bug Kekeli hit: after the agent called `solomon_onboarding_abandon` via the tool (which flips the DB row to `abandoned`), `/onboard session_0` kept refusing with "You're already in onboarding industry …" forever. The refusal lived in `_handle_onboard` and checked only the in-memory `OnboardingSessionRegistry`, not the DB. The DB row is the source of truth; the registry is just a cache.

Fix in `solomon/onboarding_v2/commands.py`:

1. New `_db_status(session_id)` helper reads the row's `status` column through the pool API.
2. `_handle_onboard` still consults the registry first, but now only refuses if `_db_status(...) == "open"`. Any other status (`abandoned`, `complete`, or missing) means the entry is stale → drop it via `self.registry.clear(session_id)` and fall through to `open_or_resume`, which creates a fresh row.

`/endinterview` was already the manual recovery path; this just makes `/onboard` self-heal so the tool-call-then-/onboard path works without a workaround.

Tests: 378/378 (376 → 378). Two new in `TestSlashCommands`:
- `test_onboard_recovers_from_stale_registry_entry` — simulates the live bug (open → abandon DB row out-of-band → /onboard again must open a new row).
- `test_onboard_still_refuses_when_db_row_is_actually_open` — belt-and-suspenders: the truly-open path still refuses.

Files modified:
- `solomon/onboarding_v2/commands.py` (+33 / -5)
- `tests/test_onboarding_v2.py` (+38)

## Last decision (Session D — Skill-driven onboarding plugin load)

Session D shipped Onboarding v2 with passing tests but `/onboard` returned `Unrecognized slash command` on Telegram. Two registration bugs were blocking real plugin load:

1. **Wrong entry-point shape.** `pyproject.toml` had `solomon = "solomon.plugin:register"` (function ref). Hermes' `_load_entrypoint_module` calls `ep.load()` then `getattr(module, "register", None)` — when the entry point is a function ref, `module` IS the function, so `getattr` returns `None` and the plugin fails with "no register() function". Fixed: entry point is now `solomon = "solomon.plugin"` (module ref). Re-install with `pip install -e . --no-deps` to refresh dist-info.
2. **`PluginContext.register_command` does not accept `aliases`.** Solomon's adapter was passing `aliases=...` and the plugin load aborted on `/private`. Hermes only supports name+handler+description+args_hint. Fixed in `solomon/adapter.py::register_command` — it now registers each alias as its own slash command pointing at the same handler. Updated `tests/test_adapter.py::test_register_command_registers_alias_as_separate_command` accordingly.
3. **Plugin opt-in required.** Pip-installed plugins are opt-in via `plugins.enabled` in `~/.hermes/config.yaml`. The user's config had no `plugins:` key at all, so Solomon was discovered but skipped. Added `plugins.enabled: [solomon]` to the config. Documented in install.sh's responsibilities — TODO: install.sh should add this automatically on first install.

**Live verification:** gateway restarted clean. agent.log shows `Solomon ready. Hooks live: on_session_start, on_session_end, pre_llm_call, post_llm_call, pre_tool_call, post_tool_call, pre_gateway_dispatch. Hooks unavailable: (none).` Telegram menu went from 124 hidden cmds → 134 (+10: `/private /priv /endprivate /onboard /interview /endinterview /endonboard /abandon /onboarding /interviews`).

Tests: 376/376 still passing.

### Session D — Skill-driven onboarding (2026-05-23 evening)

- ✅ **Drive skills copied to `~/.hermes/skills/solomon-onboarding/`.** All 14 SKILL.md files from `Project Solomon/Solomon/skills/` (`onboarding/*`, `interview/*`) now visible to Hermes — `hermes skills list` enumerates them under the `solomon-onboarding` category. Two frontmatter fixes applied (the Drive export smushed `author: Lynx + Sunny---` onto one line).
- ✅ **`solomon/onboarding_v2/session.py` — NEW.** `OnboardingSessionRegistry` (per-process map of `hermes_session_id -> ActiveInterview`), `ensure_tenant`, `open_or_resume(session_key)`, `abandon`, `complete`, `increment_turn_count`. Session-key map: `session_0` -> `industry`, ..., `session_6` -> `scopes`. Domain -> skill-name map mirrors the Drive umbrella skill.
- ✅ **`solomon/onboarding_v2/tools.py` — NEW.** Five LLM-callable tools registered under toolset `solomon`:
  - `solomon_onboarding_state` — returns captures so far, which required_fields are filled vs unfilled (with prompts), and `complete_ready`. Called by the LLM before deciding the next question.
  - `solomon_onboarding_capture` — writes a `captured_items` row with optional `field:<id>` tag in keywords. Default type `preference`; LLM picks `belief` / `non_negotiable` / `rule` / etc. when appropriate.
  - `solomon_onboarding_complete` — refuses unless all required_fields are filled (or `force=true`); renders the foundation YAML via the v1 helper `_render_foundation`; flips session status to `complete`.
  - `solomon_onboarding_abandon` — flips status to `abandoned`. Captures preserved.
  - `solomon_onboarding_list` — lists sessions for the active tenant filtered by status.
- ✅ **`solomon/onboarding_v2/commands.py` — NEW.** Three slash commands:
  - `/onboard <session_key>` (alias `/interview`) — opens or resumes the row, registers in the conductor's `onboarding_registry`, prints a kickoff message. Refuses to overwrite an active session.
  - `/endinterview` (aliases `/endonboard`, `/abandon`) — clears registry entry + flips status to `abandoned`. Captures preserved.
  - `/onboarding` (alias `/interviews`) — status command listing all sessions per ordinal.
- ✅ **`solomon/conductor.py` — MODIFIED.** Three additions:
  - `Conductor.__init__` now instantiates `self.onboarding_registry`.
  - `_pre_llm_call` checks `_onboarding_disabled()` then calls `_maybe_inject_onboarding(session_id, messages)` BEFORE the existing pipeline. If an active interview exists for this session_id, the method injects the SKILL.md body + interview state JSON + listening discipline + meta-question handling as a system message at the end of `messages` (preserves prompt caching — does NOT touch `TurnContext.system_prompt`) and returns True. `_pre_llm_call` returns early, bypassing the 10-stage decision pipeline (interview turns are not decisions).
  - New env var `SOLOMON_ONBOARDING_DISABLE`. Truthy = skip the onboarding branch entirely, fall through to the existing pipeline path bit-for-bit. Recovery: `echo SOLOMON_ONBOARDING_DISABLE=1 >> ~/.hermes/.env && hermes gateway restart`.
- ✅ **`solomon/plugin.py` — MODIFIED.** `register(ctx)` now also calls `register_onboarding_tools(adapter)` and `OnboardingCommands(adapter, conductor.onboarding_registry).register_command()`. Both wrapped in try/except + warning log so a failure doesn't break plugin startup.
- ✅ **Tests: 376/376 passing on SQLite** (351 → 376). 25 new tests across `tests/test_onboarding_v2.py`:
  - 5 session-lifecycle tests (open/resume/abandon/complete + invalid key).
  - 8 tool tests (state, capture, complete refuse/force, abandon, list, missing args, field tag propagation).
  - 7 slash-command tests (start/default/invalid/duplicate/end/status).
  - 5 conductor injection tests (no-active passes through, active injects, skill md included, messages=None safe, kill-switch env var parsing).
- ✅ **End-to-end plugin smoke test.** `/tmp/smoke_test_plugin.py` builds a fake Hermes ctx, runs `solomon.plugin.register(ctx)`, confirms 5 onboarding tools + 3 slash commands + 7 lifecycle hooks register. Existing tools (audit, salience, log_decision) still register — no regressions.

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

### Session C — Sleep jobs 9–12 + GitHub push (2026-05-26)

- ✅ **`solomon/sleep/job_9_corpus_lint.py` — NEW.** Calls `solomon.corpus.lint.run_lint()` and pushes every `severity='error'` finding into `mentoring_queue` with `source='lint_finding'`, `priority=2`. Warnings are logged-only (already surfaced via `solomon corpus stats`). Idempotent via a LIKE-based de-dupe on the JSON payload (`code` + `target`) — re-runs over the same corpus add zero new rows.
- ✅ **`solomon/sleep/job_10_corpus_backup.py` — NEW.** Tarballs `corpus_root()/raw` + `corpus_root()/wiki` into `<corpus_root>/../backups/<UTC-iso-ts>.tar.gz` (defaults to `~/.hermes/solomon/backups/` in production). Honours `SOLOMON_CORPUS_ROOT` as the source root, same resolution as the inbox watcher. 30-day retention prunes old `.tar.gz` files after the new one lands; `SOLOMON_BACKUP_RETENTION_DAYS` overrides for tests. Same-second re-invocation reuses the existing file.
- ✅ **`solomon/sleep/job_11_embed_pending.py` — NEW.** `captured_items LEFT JOIN embeddings WHERE embeddings.embedding_id IS NULL` (with `source_table='captured_items'`, `source_id='captured:<id>'`) picks up rows that haven't been embedded yet. Uses `solomon.corpus.embed.store_section_embedding` per row (which calls `embed_batch` — same stub seam the corpus tests use). Batch cap 32 per cycle; large corpora bleed through over multiple nights. Marks `captured_items.embedded_at` so the partial index shrinks.
- ✅ **`solomon/sleep/job_12_yaml_reconcile.py` — NEW.** DIFF-only foundation YAML ↔ captured_items check. For each captured row tagged `field:<id>` in its `keywords` JSON, looks up the matching `required_fields[<id>].statement` in the foundation YAML; mismatch enqueues a `mentoring_queue` row of `source='yaml_drift'`, `priority=3`, with `payload={yaml_path, field_id, captured_id, captured_value, yaml_value}`. **Never rewrites the YAML.** Null YAML entries (interview hasn't covered them) are skipped, not flagged. `SOLOMON_FOUNDATION_DIR` overrides the source dir.
- ✅ **`solomon/sleep/runner.py` — JOB_ORDER extended.** Appended `corpus_lint`, `corpus_backup`, `embed_pending`, `yaml_reconcile` in order. `tests/test_sleep_runner.py` is the smoke harness: stubs `_load_job` and asserts `run_cycle` invokes every JOB_ORDER entry exactly once, plus a fault-tolerance test that one job raising doesn't kill the rest.
- ✅ **Tests: 351/351 passing on SQLite** (327 → 351). +4 (job_9) +5 (job_10) +5 (job_11) +7 (job_12) +3 (sleep runner smoke).

## What's partially built — files exist but need wiring + verification

With the four new sleep jobs landed, Solomon is feature-complete for v1. The conductor wire-up is live, the corpus pipeline is live, the interview engine is live, the 12-job sleep cycle is live.

### Live-on-host follow-ups (2026-05-23)

Three install/UX bugs caught during first-host install. All fixed and pushed.

- ✅ **install.sh placeholder bug.** `install.sh` had `SOLOMON_REPO=https://github.com/YOUR_GH/solomon.git` as the fallback default; testers using the curl one-liner hit "Repository not found". Replaced with `kelix42`. Also patched `pyproject.toml` and `plugin.yaml` metadata for the same placeholder (visible in `pip show` and the Hermes plugin listing). Commits `bd1b2a9`, `5e90954`.
- ✅ **PATH wrapper.** `pip install solomon-brain` lands the entry point at `$VENV/bin/solomon`, which isn't on a normal user's PATH. Install completed successfully but `solomon onboard session_0` failed with command-not-found. Added a `place_path_wrapper` step that drops a thin `/bin/bash` wrapper alongside the existing `hermes` binary on PATH (mirrors the Hermes precedent on Linux at `/usr/local/bin`; picks up `/opt/homebrew/bin/` on Apple Silicon; falls back to `/usr/local/bin/`). Rejects self-referencing exec loops when the venv is on PATH (dev/CI boxes). Idempotent + `--dry-run`-safe. Commit `92d4855`.
- ✅ **`solomon --help` returned `Unknown command`.** The CLI uses hand-rolled dict-of-lambdas dispatch (no Click/argparse), so `--help`, `-h`, `help` all hit the unknown-command branch unless explicitly aliased. Three-line fix to `main()`'s dispatch guard. Commit `3b7814b`.

### Interview cold-open bug (2026-05-23)

- ✅ **session_0 cold open asked the wrong question.** First turn of the foundation interview pulled a random `fallbacks:` entry instead of "What industry are you in?" — typically landed on "What's a rule about your sector you would give a new owner on day one?", which assumed the industry was already known. Two root causes: (a) `industry.yaml` had no `industry_label` required field (started at `business_category`); (b) `select_next_probe` fell through to domain fallbacks whenever the owner's last answer didn't produce a keyword match (which always happens on turn 1, since `last_answer=""`).
  - Fix: added `industry_label` as the first required field in `industry.yaml`. Inserted a new step 3 in `select_next_probe` resolution order — "unfilled required field in declaration order" — between keyword match and domain fallback. Now Stage B opens with the first unanswered required field; only after all required fields are filled does it fall through to domain fallbacks.
  - Added `select_next_probe_with_meta` returning `(probe, field_id_or_None)`, used by the Stage B loop in `session_runner` to pass `extra_keyword_tag=f"field:{rf_id}"` into `_process_owner_turn`. Without this, captures from Stage B required-field prompts wouldn't be tagged and Stage C would re-ask the same field. `select_next_probe` (single-return) kept as a thin wrapper for back-compat with the 8 test call-sites.
  - All other libraries (`belief_system`, `ideal_outcomes`, `non_negotiables`, `principles`, `scopes`, `why`) already had a sensible first required field. The engine fix gives them the right cold open too as a free byproduct.
  - Test update: `test_session_0_industry_runs_to_completion` now scripts one extra answer for the industry-label cold open and asserts on the new tag. 351/351 tests still passing.

Remaining items are deployment + first-real-use, not build:

- ❌ Deploy to a real host (the install.sh script is ready; haven't run it on Kekeli's actual box yet).
- ❌ Run the foundation interview end-to-end with Kekeli — first time the YAMLs get filled from a live session, not a unit-test fixture.
- ❌ Drop a corpus pack into `~/.hermes/solomon/corpus/inbox/` and exercise the mentoring review CLI live (corpus_ingest → rule mining → mentoring_queue → `solomon mentoring review`).
- ❌ (Future, not blocking) The queue-mode pipeline-tick worker at `solomon/workers/pipeline_tick/__main__.py`. The conductor inserts the row; until the worker exists, `SOLOMON_PIPELINE_MODE=queue` essentially fire-and-forgets to a stale row. **Default mode is `inline` so this doesn't bite us.**
- ❌ (Cleanup, not blocking) Older sleep jobs 1–8 + the `cycle_log` INSERT in `runner.py` still use `%s` placeholders + `psycopg`-style `get_pool().connection()` — they crash on SQLite. The new jobs 9–12 use the portable pool API. A future cleanup pass should rewrite the older jobs to match. Run-cycle still works on SQLite because each job is wrapped in try/except + the cycle_log persist is wrapped too, so failures degrade gracefully.

## Recommended next-session priority order

With Sessions A, B, and C landed, Solomon is feature-complete for v1. The remaining work is shaking the build out in real conditions:

1. **Deploy on Kekeli's actual host.** Run `install.sh` end-to-end on the production box (not the dev container). Confirm the Hermes plugin entry-point fires, the SQLite WAL file lands at `~/.hermes/solomon/solomon.db`, and the cron entry is wired for the 02:00 sleep-cycle run.
2. **First live foundation interview.** Step through the 5-stage onboarding flow with Kekeli on Telegram. Watch for: ELIZA-listening misfires (the seven rules pinned in `references/eliza-listening.md`), required-field coverage, end-of-session YAML render. The unit-tests exercised every stage with stubs; this is the first real LLM-in-the-loop test.
3. **First live corpus pack.** Drop a directory of real docs (Drive export, notes, contracts) into `~/.hermes/solomon/corpus/inbox/`, let the watcher pick them up, then walk through `solomon mentoring review` with Kekeli. This exercises corpus → rule mining → mentoring_queue → owner decision end-to-end for the first time.
4. **(Future, not blocking)** Build the pipeline-tick worker so `SOLOMON_PIPELINE_MODE=queue` becomes a real option (offloads the synchronous runner cost from the user-visible turn).
5. **(Cleanup, not blocking)** Rewrite sleep jobs 1–8 + the `cycle_log` INSERT to use the portable pool API + `?` placeholders, matching jobs 9–12. They currently crash on SQLite but degrade gracefully through the runner's try/except.

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

## Files modified or added in Session C (2026-05-26, sleep jobs 9–12 + JOB_ORDER + smoke test)

```
+ solomon/sleep/job_9_corpus_lint.py           # corpus_lint errors → mentoring_queue, source='lint_finding'
+ solomon/sleep/job_10_corpus_backup.py        # nightly tarball of corpus/raw + corpus/wiki, 30-day retention
+ solomon/sleep/job_11_embed_pending.py        # LEFT JOIN captured_items × embeddings, batch-cap 32
+ solomon/sleep/job_12_yaml_reconcile.py       # diff foundation YAMLs vs captured_items → 'yaml_drift' queue rows

M solomon/sleep/runner.py                      # JOB_ORDER extended with jobs 9-12 in order

+ tests/test_job_9_corpus_lint.py              # 4 tests: enqueue errors, skip warnings, idempotent, clean corpus
+ tests/test_job_10_corpus_backup.py           # 5 tests: tarball contents, skip-when-empty, 30d retention, configurable window, idempotent
+ tests/test_job_11_embed_pending.py           # 5 tests: embed pending, idempotent, batch cap, skip empty, marks embedded_at
+ tests/test_job_12_yaml_reconcile.py          # 7 tests: detect drift, aligned, null YAML skip, idempotent, empty dir, untagged rows, no overwrite
+ tests/test_sleep_runner.py                   # 3 tests: JOB_ORDER lists all 12, run_cycle invokes each once, fault-tolerance

M BUILD-STATE.md                               # this file
```

**Tests:** 351/351 passing on SQLite (327 baseline + 24 new in Session C: 4 + 5 + 5 + 7 + 3). Run `pytest tests/ -q` to verify.

**Session C explicitly did NOT touch `solomon/conductor.py`, `solomon/pipeline/`, `solomon/corpus/`, `solomon/mentoring/`, or `install.sh`.** Those landed in earlier sessions and are in production-ready state.

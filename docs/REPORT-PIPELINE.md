# REPORT — Decision Pipeline / Sleep Cycle / Autonomy / Hermes Integration

Comparison of the **Drive version** (`/root/projects/solomon-from-drive/`, the
spec-first scaffold) vs the **Built version** (`/root/projects/solomon/`, the
working Python implementation). Goal: take the best of both for the merged
single project.

---

## 1. What the Drive version has

### 1.1 Architecture (10-stage pipeline, worker-driven)

The Drive version does **not** run the pipeline inline inside a Hermes hook. It
runs the pipeline in a **separate OS-supervised worker** (`pipeline-tick`) that
polls a SQLite event queue.

```
[ Telegram ] → Hermes gateway
                  ↓ pre_llm_call hook (solomon-pipeline-injector)
              INSERT INTO events(status='pending') ──── persists ────→ db/solomon.db
                  ↓ (Hermes proceeds with normal LLM call, hook returns None)

[ pipeline-tick worker, every 60s ]
   SELECT … WHERE status='pending' LIMIT (MAX_IN_FLIGHT - in_flight)
   atomic UPDATE … SET status='in_progress'
       ↓ spawn thread
   orchestrator.pipeline.runner.run(event_id)   ← 10 stages, async from the gateway turn
```

**The user-facing Telegram turn is NOT blocked on the pipeline.** The pipeline is
a *shadow* decision-making loop that writes back to `db.events`, `db.decisions`,
`db.audits`, and (Stage 10) routes outbound actions (ship / one-tap /
suggestion / escalate).

#### The 10 stages (`orchestrator/pipeline/stage_*.py`)

| # | Stage | File | Model | Notes |
|---|---|---|---|---|
| 1 | Capture | `stage_capture.py` | none | Validates `db.events` row exists |
| 2 | Salience | `stage_salience.py` | Haiku, ~80 tok | JSON `{stakes, novelty, emotion, owner_involvement, combined}`; `combined = max(...)`. If `< 0.30` → `status='skipped'`, **pipeline halts** |
| 3 | Classification | `stage_classification.py` | Sonnet, ~120 tok | JSON `{scope, domain, decision_type}` |
| 4 | Hard-rule | `stage_hard_rule.py` | **deterministic, no LLM** | Reads `foundation/05-non-negotiables.yaml` `rules:` list; each has a JSON-logic `condition` evaluated via `json_logic.jsonLogic(condition, data)`. On match → `status='blocked_by_hard_rule'`, halts |
| 5 | Retrieval | `stage_retrieval.py` | none | 5-lane: Pinecone semantic + recency + entity + pressure + foundation. Currently stubbed |
| 6 | System 1 | `stage_system1.py` | Sonnet, ~200 tok | Rules-only, 1–2 sentences |
| 7 | System 2 | `stage_system2.py` | Opus, ~2000 tok | Chain-of-thought |
| 7b | Divergence | inside `stage_system2.py` | none | `0.6·jaccard + 0.4·length_ratio`, token-based (NOT embeddings) |
| 8 | Audit | `stage_audit.py` | Opus, ~300 tok | Independent gate. Returns `APPROVE / DOWNGRADE / REJECT / REQUEST_RETHINK` |
| 9 | Owner-state | `stage_owner_state.py` | deterministic, reads `db.biometrics` | green/yellow/red/unknown from `recovery_pct`, `sleep_hours`, `stress_flag` |
| 10 | Action | `stage_action.py` | none | `min(scope_autonomy.level, ceil_by_state[owner_state])` × verdict → routes the four action types |

Key infra: `_helpers.py` has WAL-mode SQLite connect, `update_event(**fields)`, a
`stage_timer` context manager that writes per-stage elapsed_ms into the JSON
column `events.stage_timings_ms`, and an `llm_dispatch(model_env, ...)` shim
keyed on env vars `SOLOMON_MODEL_{SALIENCE,CLASSIFICATION,SYSTEM1,SYSTEM2,AUDIT}`.

Order matters: **hard-rule is Stage 4, after salience+classification but BEFORE
retrieval+S1+S2+audit.** This saves three LLM calls when a violation fires.

### 1.2 Sleep cycle — 12 jobs

`orchestrator/sleep-cycle/job01_*.py` … `job12_*.py`. All are stub scaffolds
right now (each `run()` is a `pass` body with try/except). The README lists:

1. hindsight  — audit past 24h decisions, write `db.audits` rows
2. archival   — move stale items to long-term storage
3. surprise-replay — divergence < 0.7 events → mentoring_queue priority 4
4. stress-test — simulated edge cases vs current rules
5. conflict-detection — cross-heuristic conflicts → mentoring_queue priority 3
6. working-memory-cleanup — trim past TTL
7. autonomy-reeval — promotion / demotion of `db.scope_autonomy`
8. mentoring-scheduler — trigger session if priority ≤ 4 queued OR 7d elapsed
9. **corpus-lint** — contradictions, stale, orphans, near-duplicates
10. **corpus-backup** — snapshot db+corpus, AES-256-GCM, ship to `BACKUP_DEST_LOCAL`
11. **embed-pending** — captured_items + decisions WHERE `embedded_at IS NULL` → OpenAI batch → Pinecone upsert
12. **yaml-reconcile** — re-render 7 foundation YAMLs from captured_items; drift → mentoring_queue priority 5

Scheduled via Hermes gateway built-in cron (`/cron add`), default
`0 3 * * *` owner-local. Owner overrides: `/solomon-sleep-now`,
`/solomon-sleep-job <name>`, `/solomon-sleep-skip <name>`. Each job runs as a
fresh isolated Hermes agent session with its matching skill attached. Failure
of one does not block the others.

### 1.3 Autonomy — L0–L4 per scope, biometric ceiling

`db/schemas/scope_autonomy.sql`:
```sql
scope TEXT PRIMARY KEY, level INTEGER CHECK(level BETWEEN 0 AND 4),
since TEXT, last_reeval_at TEXT, override_rate_30d REAL, audit_pass_rate_30d REAL
```

Levels (`references/autonomy-spectrum.md`):
- **L0 Manual** — does nothing automatic
- **L1 Suggested** — proposes, owner approves every action
- **L2 Drafted** — drafts and ships only after one-tap
- **L3 Supervised** — ships routine, novel still needs approval
- **L4 Autonomous** — ships everything, daily digest

Thresholds (Job 7 autonomy-reeval):
- **Promotion**: ≥20 events in scope over trailing 30d AND override-rate < 10% AND audit-pass > 90% → `level + 1`
- **Demotion**: override-rate > 25% OR hard-rule violation → `level - 1`
- Each transition writes `decisions/log.md`

**Owner-state ceiling** (Stage 9): the per-event ceiling caps the scope level:
- Green (recovery > 60% AND sleep > 7h): L4
- Yellow (recovery 33–60% OR sleep 5–7h): L2
- Red (recovery < 33% OR stress_flag): L1
- Whoop missing / stale > 24h: default Green + warn-once

Effective = `min(scope_level, ceiling)`.

### 1.4 Hermes integration

Two-pronged:

1. **`solomon-pipeline-injector` plugin** (`hermes-plugins/solomon-pipeline-injector/__init__.py`, 56 lines) — single `pre_llm_call` hook. On `platform == "telegram"` AND `is_first_turn`: opens its own WAL SQLite connection, inserts a `db.events` row with `status='pending'`, returns `None` so Hermes proceeds normally. **Telegram-only, first-turn-only.** No PluginContext fanciness — uses only `ctx.register_hook`.

2. **`workers/pipeline-tick`** — long-lived OS-supervised process (systemd unit at `install/supervisor/solomon-worker.template.service`). Polls every 60s, atomic claim, threads out to `pipeline.runner.run(event_id)`. Cap `PIPELINE_MAX_IN_FLIGHT=5`. Source-dispatched: `file_dropped` → `corpus_ingest.ingest.run_for_event` (no LLM); everything else → the decision pipeline. SIGTERM-aware (drains in-flight before exit).

3. **`install.sh`** — 222 lines, single supported install command. Symlinks repo to `~/.hermes/skills/solomon`, symlinks each `hermes-plugins/*` into `~/.hermes/plugins/`, initialises `db/solomon.db` from `db/schemas/*.sql` with WAL pragmas, prompts only for missing keys (`PINECONE_API_KEY`, `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), idempotent re-verify path, `--restore <tarball>` path, BIP-39 24-word backup-key flow, first-entry `decisions/log.md`, optional Session 0 auto-launch.

The Drive version explicitly states (`CLAUDE.md`): **`hermes skills install <repo>` is NOT supported** — that path skips DB init, plugin symlinks, backup-key flow. Only `bash install.sh`.

### 1.5 Events schema (the contract the pipeline writes to)

```sql
event_id TEXT PRIMARY KEY,          -- ulid
source TEXT,                        -- telegram | plaud_live | whoop | gmail_live | file_dropped | ...
payload TEXT,                       -- JSON
received_at, processed_at TEXT,
salience_score REAL,
classification TEXT,                -- JSON {scope, domain, decision_type}
hard_rule_blocked TEXT,             -- rule_id or NULL
system1_output, system2_output TEXT,
divergence_score REAL,              -- token-Jaccard
audit_verdict TEXT,                 -- APPROVE | DOWNGRADE | REJECT | REQUEST_RETHINK
owner_state TEXT,                   -- green | yellow | red | unknown
action_taken TEXT,
status TEXT CHECK(IN pending|in_progress|complete|skipped|failed|blocked_by_hard_rule),
stage_timings_ms TEXT               -- JSON per-stage elapsed
```

Every stage `UPDATE`s columns on the row; the row IS the audit trail. No
separate per-stage tables.

---

## 2. What we built

### 2.1 Architecture (7-step in-conductor pre_llm_call)

`solomon/conductor.py` — single class, single thread of execution. Hooks attach
via `adapter.attach_all({...})`, then `_pre_llm_call` runs steps 1–10 inline
**before** the LLM call returns to Hermes. There is no event queue, no worker,
no async decoupling — the LLM call blocks until the conductor's pipeline
finishes.

Steps inside `_pre_llm_call`:
1. **Classify** (`classify/classifier.py`) → `(scope, domain, decision_type, confidence)`
2. **Salience** (`salience/scorer.py`) → `(score, breakdown)`
3. **Non-negotiable check** (`non_negotiables/check.py`) — keyword/regex/llm; if violated → set `audit_verdict="reject"` and *return early* (skips retrieval/S1/S2/audit)
4–5. **Working memory + retrieval** (`memory/working.py`, `memory/retrieval.py`) — only runs retrieval if `hot.is_thin()`
6. **System 1** (`reasoning/system_1.py`)
7. **System 2** (`reasoning/system_2.py`)
8. **Divergence** (`reasoning/divergence.py`) — pure Jaccard distance (no length ratio)
9. **Autonomy lookup** (`autonomy/ladder.py`)
10. **Audit gate** (`audit_gate/audit.py`) → `AuditVerdict(verdict, reasoning, checks_passed, checks_failed)`

Then `_post_llm_call` finalises the turn: writes to `decision_log`, stores
prediction + counterfactual if `salience >= 0.4`, drops `TurnContext`.

Storage: **Postgres** (`storage/pool.py`), not SQLite. Tables include
`autonomy_state`, `decisions`, `tenants`, `cycle_log`.

### 2.2 Sleep cycle — only 8 jobs

`solomon/sleep/runner.py` walks `JOB_ORDER`:

1. hindsight
2. rule_archival
3. surprise_replay
4. stress_test
5. conflict_detection
6. working_memory
7. autonomy
8. mentoring_scheduler

**Missing vs Drive**: J9 corpus-lint, J10 corpus-backup, J11 embed-pending,
J12 yaml-reconcile. (Our build was scoped to brain-only; corpus + backup +
embedding lived outside.)

Per-job result schema: `{items_processed, tokens, status, duration_s, reason}`.
Persisted to `cycle_log` table with `per_job JSONB`.

### 2.3 Autonomy — 4 levels, no biometric ceiling

`solomon/autonomy/ladder.py`:
```python
AUTONOMY_LEVELS = ("watch", "suggest", "act_with_approval", "act_alone")
OBSERVE_ONLY_DAYS = 30
```

Same shape as L0–L4 (renamed). Thresholds (Job 7):
- **Demotion**: `non_negotiable_violations_7d > 0` → drop to `watch`; OR `override_rate_7d > 0.15` → -1 level; OR `edit_rate_7d > 0.30` → -1 level
- **Promotion** (flagged, **not** auto-applied — needs owner one-click): `decision_count_30d >= 50` AND `override_rate_30d < 0.05` AND `avg_confidence_30d > 0.8`
- Hysteresis: 14-day cooldown after last promotion/demotion
- 30-day observe-only window for new tenants

**No owner-state / Whoop ceiling.** Effective autonomy = scope level, period.

### 2.4 Non-negotiables — keyword / regex / LLM

`solomon/non_negotiables/check.py` reads
`~/.hermes/solomon/foundation/non_negotiables.yaml` (a list of
`{name, scope, check_type, check_pattern, description}` rules). `check_type` is
one of:
- `keyword` — substring match
- `regex` — `re.search`
- `llm` — sends to Opus, expects `{"violation": bool, "reason": str}`

**No JSON-logic, no structured event data.** It only sees `raw_event.raw_content`
as text. There's no way to express "scope == pricing AND margin_pct < 15".

### 2.5 Hermes integration

`solomon/plugin.py` (79 lines) — `register(ctx)` wraps `ctx` in
`HermesAdapter`, calls `init_storage()`, builds `Conductor`, registers tools,
attaches hooks via `adapter.attach_all({...})`.

`solomon/adapter.py` — clean abstraction over `ctx.register_hook /
register_tool / register_command / get_config`. Declares `REQUIRED_HOOKS` (the
six basics) and `OPTIONAL_HOOKS` (`pre_gateway_dispatch`,
`transform_llm_output`, `pre_approval_request`, `post_approval_response`).
Missing hooks degrade gracefully — `HookAttachment(attached=False, reason=...)`
is captured per-hook; required hooks raise `AdapterError`.

The conductor attaches to: `on_session_start`, `on_session_end`,
`pre_llm_call`, `post_llm_call`, `pre_tool_call`, `post_tool_call`,
`pre_gateway_dispatch`. Compared to Drive's plugin: ours subscribes to many
more hooks, ours runs the brain inline (blocking) instead of queueing.

`install.sh` is much shorter (113 lines) — pip-installs `solomon-brain` into
the Hermes venv, runs `solomon init`, restarts the gateway.

---

## 3. Best-of-both recommendation

| Concern | Adopt from | Why |
|---|---|---|
| **10-stage pipeline order** | Drive | Hard-rule at Stage 4 (before retrieval / S1 / S2 / audit) saves 3+ LLM calls on every violation. Salience-skip at < 0.30 saves money too. Ours runs retrieval + S1 + S2 always |
| **Pipeline organisation: split into `stage_*.py`** | Drive | One file per stage = easy unit testing, easy reordering, easy to stub a stage. Our 280-line `_pre_llm_call` is one big try/except |
| **Per-event row as the audit trail** | Drive | `db.events.stage_timings_ms` + per-stage column updates is a clean log shape. We currently bury most of this in `TurnContext` (transient) and only persist a flattened `decisions` row |
| **JSON-logic non-negotiables** | Drive | Lets rules reference structured event data (scope, margin_pct, etc.), not just `raw_content` text. Our keyword/regex/LLM check can't express "below 15% margin on commercial work". Use `json-logic-py` PyPI package |
| **L0–L4 naming + 5 levels** | Drive | L0 (Manual, "only when asked") is a real state we don't have. Our 4 levels collapse Manual+Watch into one. Rename our `watch/suggest/act_with_approval/act_alone` → `L0/L1/L2/L3/L4` and add explicit L0 |
| **Promotion/demotion thresholds** | Drive's are stricter on count, ours on rates | Hybrid: ≥20 events (Drive) AND override < 10% AND audit-pass > 90%. Demote on > 25% override OR hard-rule violation. Keep our 14-day hysteresis and owner-approve-promotion (don't auto-promote) |
| **12-job sleep cycle** | Drive | Port J9 corpus-lint, J10 corpus-backup, J11 embed-pending, J12 yaml-reconcile into `solomon/sleep/`. Append to `JOB_ORDER` |
| **Token-Jaccard divergence formula** | Drive uses `0.6·jaccard + 0.4·length_ratio` | Slightly better than pure Jaccard — paraphrases of different length get distinguished. Drop into `reasoning/divergence.py` |
| **Hermes plugin shape** | Ours (`adapter.py` + `register_hook` via attach_all) | Cleaner. Drive's plugin uses ctx directly and only does one hook. Our `HermesAdapter` is the right abstraction — keep it, but **add the queue-on-pre_llm shape** from Drive as a *mode* (synchronous-inline vs queue-and-tick) |
| **Worker-driven `pipeline-tick`** | Drive — but make it OPTIONAL | For Telegram messages, the inline conductor blocks the LLM response on 7 LLM calls (salience+classify+s1+s2+audit ≈ 5–15 sec). The Drive's async-queue model lets the gateway reply immediately while the pipeline shadow-runs. Recommend: keep the inline conductor as the primary path; add `pipeline-tick` worker for `source IN (plaud, gmail, calendar, file_dropped, webhook)` events that arrive outside a gateway turn |
| **Telegram-only pre_llm_call write-to-events** | Drive | Even with inline mode, drop an `events` row on each gateway turn for crash recovery + audit trail. Use `ulid` not `uuid4` |
| **Whoop / biometrics / owner-state gate** | **SKIP for v1** | Drive's own `SOLOMON-PLAN.md` puts Whoop at Tier 3 (optional integrations menu — unchecked by default). The schema and stage scaffold are nice to have but the dependency on a Whoop OAuth flow is a v2 problem. **Keep the `biometrics` table and `owner_state` event column for future, but Stage 9 returns "unknown" (= full ceiling) until Whoop ships** |
| **install.sh** | Drive's flow (symlink, WAL DB, BIP-39, optional integrations menu) — but use our pip-installable shape | Drive's is bash-only and assumes a checked-out repo. Ours is pip-distributable but skips the WAL DB init + plugin symlinks + backup-key. Merge: pip install solomon-brain → `solomon init` runs the Drive 12-step flow internally |
| **SQLite WAL vs Postgres** | **Tension** — Drive uses SQLite WAL, ours uses Postgres | SQLite WAL is single-machine, zero-install, perfect for personal use. Postgres adds an install dependency. For a *personal* business brain (single owner), SQLite WAL wins. **Recommend: switch storage default to SQLite WAL, keep Postgres as an opt-in via `SOLOMON_DB_URL`.** This unblocks the worker model too — Postgres + the `pipeline-tick` worker is a heavier ops setup |

---

## 4. Concrete integration plan

Target tree under `/root/projects/solomon/solomon/`. The conductor stays as
the in-process entry point; the 10 stages become first-class modules; the
worker becomes optional.

### 4.1 New / changed files

```
solomon/
  pipeline/                          ← NEW package (port from orchestrator/pipeline/)
    __init__.py
    _helpers.py                      ← db_connect (SQLite WAL), update_event, stage_timer, llm_dispatch
    runner.py                        ← run(event_id) — walks 10 stages
    stage_capture.py                 ← validates events row exists
    stage_salience.py                ← refactor solomon/salience/scorer.py to fit
    stage_classification.py          ← refactor solomon/classify/classifier.py
    stage_hard_rule.py               ← NEW — JSON-logic eval of foundation/05-non-negotiables.yaml
    stage_retrieval.py               ← refactor solomon/memory/{working,retrieval}.py
    stage_system1.py                 ← thin wrapper over solomon/reasoning/system_1.py
    stage_system2.py                 ← wrapper + token-Jaccard + length-ratio divergence
    stage_audit.py                   ← thin wrapper over solomon/audit_gate/audit.py
    stage_owner_state.py             ← reads db.biometrics; returns "unknown" → no-op ceiling for v1
    stage_action.py                  ← effective_autonomy = min(scope_level, ceil_by_state); routes ship/one-tap/suggest/escalate

  conductor.py                       ← MODIFY: _pre_llm_call now (a) inserts events row, (b) calls pipeline.runner.run(event_id) (in-process), (c) reads the row back to populate TurnContext
                                     ← Add config flag SOLOMON_PIPELINE_MODE = "inline" (default) | "queue"
                                     ← In "queue" mode, just insert events row + return; pipeline-tick worker handles it

  storage/
    schema.sql                       ← MODIFY: add events table (port from db/schemas/events.sql)
                                     ← Add biometrics, scope_autonomy tables (port from Drive)
                                     ← Add stage_timings_ms JSON column
    pool.py                          ← MODIFY: add SQLite WAL backend (default), keep Postgres as fallback via DSN sniff

  non_negotiables/
    check.py                         ← REPLACE: drop keyword/regex/llm modes,
                                                 add JSON-logic mode reading
                                                 foundation/05-non-negotiables.yaml `rules:` list with `condition`
                                                 (still keep `check_type: llm` as escape hatch for fuzzy rules)
    jsonlogic.py                     ← NEW thin wrapper around json-logic-py

  autonomy/
    ladder.py                        ← MODIFY: rename levels to L0/L1/L2/L3/L4
                                                add owner_state_ceiling(state) → int
                                                effective_for(scope, owner_state) → int

  sleep/
    runner.py                        ← MODIFY: extend JOB_ORDER with 4 new jobs
    job_9_corpus_lint.py             ← NEW (port semantics from Drive scaffold)
    job_10_corpus_backup.py          ← NEW
    job_11_embed_pending.py          ← NEW
    job_12_yaml_reconcile.py         ← NEW

  workers/                           ← NEW (optional; only built if SOLOMON_PIPELINE_MODE=queue)
    pipeline_tick/__main__.py        ← port from solomon-from-drive/workers/pipeline-tick/__main__.py

  hermes_plugin/
    plugin.py                        ← unchanged (Hermes entry point)
    adapter.py                       ← unchanged (it's already cleaner than Drive's)

foundation/
  05-non-negotiables.yaml            ← NEW canonical location, JSON-logic format

install.sh                           ← REWRITE: merge our pip-install shape with Drive's 12-step flow
                                                 (DB init, plugin symlinks, BIP-39 backup key,
                                                 optional integrations menu, decisions/log.md first entry)
```

### 4.2 Should the pipeline live in `conductor.py` or split into `stage_*.py`?

**Split.** The Drive's per-stage modules are clearly better:
- Each stage is ~20–50 lines, single responsibility, trivial to unit test.
- `pipeline/runner.py` becomes the readable contract (the 10-line `run()` that
  literally lists the 10 calls in order).
- Conductor shrinks to: prepare TurnContext → insert events row → call
  `pipeline.runner.run(event_id)` → read events row back → assemble response.
  ~80 lines instead of 280.
- Lets us flip to queue mode without touching anything except the conductor
  branch (`if mode == "queue": insert + return; else: insert + run + read`).

### 4.3 Data flow for one event end-to-end (inline mode)

```
1. Telegram message → Hermes gateway
2. ctx fires pre_gateway_dispatch → conductor stashes raw_event in TurnContext
3. ctx fires pre_llm_call → conductor:
     a. INSERT INTO events (event_id=ulid, source='telegram', payload=json,
                            received_at=now, status='pending')
     b. pipeline.runner.run(event_id)
          stage_capture.run(event_id)            → reads row
          stage_salience.run(event_id, capture)  → Haiku → UPDATE salience_score
                                                  → if < 0.30: status='skipped', halt
          stage_classification.run(...)          → Sonnet → UPDATE classification
          stage_hard_rule.run(...)               → json_logic eval
                                                  → if match: status='blocked_by_hard_rule', halt
          stage_retrieval.run(...)               → Pinecone + 5 SQL lanes
          stage_system1.run(...)                 → Sonnet → UPDATE system1_output
          stage_system2.run(...)                 → Opus → UPDATE system2_output, divergence_score
          stage_audit.run(...)                   → Opus → UPDATE audit_verdict
          stage_owner_state.run(event_id)        → reads biometrics → UPDATE owner_state (v1: always "unknown" → green ceiling)
          stage_action.run(...)                  → UPDATE action_taken, status='complete'
     c. SELECT … FROM events WHERE event_id=? → load all 14 columns into TurnContext
     d. If status IN (skipped, blocked_by_hard_rule, complete with REJECT/REQUEST_RETHINK):
          inject a system message into the conversation telling the LLM to escalate / decline
     e. Hermes proceeds with normal LLM call (Solomon's persona answers the user using the verdict + context)
4. ctx fires post_llm_call → conductor:
     - INSERT INTO decisions row (mirror of events for the H2 log format)
     - APPEND to decisions/log.md
     - Schedule prediction + counterfactual if salience >= 0.4
     - working_memory.update_after_turn
     - DROP TurnContext
```

In **queue mode**, step 3b–3d are skipped at pre_llm_call time; the pipeline-
tick worker picks up the row 0–60s later and runs the same 10 stages. The
user gets an immediate gateway response; the shadow decision is processed
behind. For Telegram one-tap routing (Stage 10), the worker pushes a Telegram
message via the gateway's outbound API.

### 4.4 Migration order (recommended)

1. **Land the schema additions first** (events + biometrics + scope_autonomy + stage_timings_ms) — non-breaking.
2. **Port `stage_*.py` modules**, wire `pipeline/runner.py`, keep conductor pointing at old in-place steps. Verify pipeline runs standalone against a manually inserted event row.
3. **Switch conductor to call `pipeline.runner.run`** in inline mode. Smoke-test that Telegram messages still flow.
4. **Replace non-negotiables** with the JSON-logic engine. Port the example rule shape from `foundation/05-non-negotiables.yaml`.
5. **Rename autonomy levels** to L0–L4 and add Stage 9 owner_state (returning "unknown" → green for now).
6. **Port the 4 missing sleep jobs** (corpus-lint, corpus-backup, embed-pending, yaml-reconcile).
7. **Add pipeline-tick worker as optional** (queue mode), behind `SOLOMON_PIPELINE_MODE=queue`. Default stays inline.
8. **Rewrite install.sh** to do the Drive 12-step flow inside `solomon init`.
9. **Switch storage default to SQLite WAL.** Keep Postgres opt-in. (Largest blast radius — do this last; it touches `storage/pool.py` and every `cur.execute` call style.)
10. **Defer Whoop biometrics integration** to a separate v2 plugin. Stage 9 stays a no-op stub.

### 4.5 What to delete

- `solomon/non_negotiables/check.py` — replaced.
- The big inline `_pre_llm_call` body in `conductor.py` — replaced by 4 lines that call `pipeline.runner.run`.
- The `keyword` / `regex` non-negotiable code paths (subsumed by JSON-logic + an `llm` escape hatch).
- The 4-level enum strings; everything refers to `L0`..`L4` ints with a small `LEVEL_NAMES` map for display.

---

## Appendix — file-by-file source verification

- Drive pipeline runner: `orchestrator/pipeline/runner.py` lines 27–52 — the 10-stage call order.
- Drive hard-rule JSON-logic call: `stage_hard_rule.py` lines 22–25 (`from json_logic import jsonLogic`).
- Drive divergence formula: `stage_system2.py` line 42 (`0.6 * jaccard + 0.4 * length_ratio`).
- Drive owner-state thresholds: `stage_owner_state.py` lines 23–29.
- Drive autonomy thresholds: `references/autonomy-spectrum.md` lines 14–16.
- Drive plugin injector: `hermes-plugins/solomon-pipeline-injector/__init__.py` lines 29–56.
- Drive worker dispatch loop: `workers/pipeline-tick/__main__.py` lines 114–140.
- Built conductor pipeline: `solomon/conductor.py` lines 160–248 (`_pre_llm_call`).
- Built autonomy ladder: `solomon/autonomy/ladder.py` line 32 (`AUTONOMY_LEVELS = ("watch", "suggest", "act_with_approval", "act_alone")`).
- Built non-negotiable check types: `solomon/non_negotiables/check.py` lines 107 (`check_type not in {"keyword", "llm", "regex"}`).
- Built sleep runner job count: `solomon/sleep/runner.py` lines 29–38 (8 entries).
- Built divergence (pure Jaccard): `solomon/reasoning/divergence.py` lines 29–56.
- Built install.sh: 113 lines vs Drive's 222.

# REPORT — Interview / Onboarding / ELIZA-listening Subsystem
## Drive version vs. what we built (under `/root/projects/solomon/`)

Date: 2026-05-22
Scope: interview + onboarding + ELIZA listening only.
Verdict up front: the Drive version has a real, well-thought-out interview engine. What we built is a placeholder Q&A loop that writes YAML. The right move is to graft the Drive architecture onto our codebase mostly intact, dropping only the parts that exist purely to serve Hermes skill packaging.

---

## 1. What the Drive version has

The interview phase is its own subsystem with **5 dedicated skills**, **3 lifecycle tables**, a **per-domain probe library**, and **8 onboarding wrappers** (one per session, plus an index and a status reporter). It is *not* a Q&A script.

### 1.1 Five interview-phase skills

All carry `phase: interview` front-matter and refuse to load in the decision pipeline. Listed in invocation order on a single owner turn:

1. **`solomon-interview-engine`** — the orchestrator.
   - Per-turn process:
     1. Read `db.clarification_queue WHERE session_id=? AND status='queued'`. Pending clarifications **jump the queue** — asked verbatim.
     2. Else detect keywords in owner's last answer.
     3. Open `probe_library/<domain>.yaml` for the active domain.
     4. Pick highest-priority **unused** probe for a matched keyword that hasn't hit `coverage.probe_count` saturation. Lower priority number wins.
     5. Render template with verbatim `{phrase}` substitution from owner's last answer.
     6. Ask **one** question. Never stack.
     7. On dry keyword → fall back to a related keyword in the same domain → then a generic forward prompt from `_generic.yaml`.
     8. After asking: `coverage.probe_count++`, `coverage.last_probed=NOW()`, `coverage.last_probed_version=<library version>`.
   - On launch: probe-library version migration check writes a `mentoring_queue` row (priority 7) if `coverage.library_version_seen < library.version`. No automatic mass re-probe.

2. **`solomon-extraction`** — Sonnet call, post-owner-turn hook.
   - Always preceded by `solomon-redact` (PII pass) on the owner's text.
   - Returns structured JSON validating against `db/schemas/captured_items.sql`.
   - Identifies each distinct claim → `domain` / `type` / `statement` / `verbatim_phrase` / `example` / `reasoning` / `conditions` / `keywords` / `confidence` and writes ≥0 rows with `embedded_at=NULL` (Sleep-Cycle Job 11 batch-embeds later).
   - **Confidence ladder is hard-coded**: `stated` (first appearance, no example), `repeated` (second+ equivalent claim), `exemplified` (claim + concrete instance).
   - Updates `coverage.items_captured`, decrements `gap_score` by `1/(probe_count+1)` per new item, resets `turns_since_last_capture=0`.
   - Triggers `solomon-contradiction-check` per inserted row.

3. **`solomon-vocabulary-capture`** — runs in parallel with extraction, also after `solomon-redact`.
   - **Two passes**:
     - Pass 1: spaCy `en_core_web_sm` POS tagging → NP and VP chunks (deterministic, free, fast).
     - Pass 2: Sonnet, ~200 tokens out, prompt: *"Extract idioms, metaphors, or stock expressions from this text. Return JSON: `[{phrase, type}]`."*
   - **Normalization rule** (canonical, in `db/schemas/vocabulary.sql`): lowercase → strip surrounding punctuation → collapse internal whitespace → strip leading/trailing articles (the/a/an). **No stemming.** Hyphens preserved. Equivalent spellings live in the `aliases` JSON column.
   - On hit: `frequency++`, update `last_seen`. Else INSERT.
   - **Not embedded** — vocabulary is queried via SQL frequency/recency, never as vectors.

4. **`solomon-coverage-tracker`** — read-only, called before probe selection.
   - Returns lowest-coverage sub-topic with `gap_score > 0.4`.
   - **Session-complete rule** (either condition):
     - Saturation: every sub-topic for the active domain has `gap_score < 0.4` AND `probe_count >= 5`.
     - Diminishing returns: total session `probe_count >= 8` AND `turns_since_last_capture >= 4`.
   - Owner overrides: `/solomon-onboarding-end` (force complete), `/solomon-onboarding-keep-going` (extend).

5. **`solomon-contradiction-check`** — Sonnet, post-extraction insert.
   - Query existing `captured_items WHERE domain=?`. For each existing row, ask Sonnet `{is_conflict: bool, conflict_type, suggested_probe}`.
   - On conflict: append the new id to both rows' `conflicts_with` JSON list, INSERT into `clarification_queue` with both ids and a suggested probe like *"Earlier you said X; just now Y. Which wins, and why?"*.
   - Resolves in the **same session** (that's why `clarification_queue`, not `mentoring_queue`).
   - **No semantic lookup — it's SQL-by-domain plus a pair-wise LLM compare.** Cheap and bounded.

### 1.2 Lifecycle tables (canonical, in `db/schemas/`)

- **`captured_items.sql`** — ULID PK, `domain`, `type ∈ {rule,exception,trigger,preference,value,story}`, `statement`, `verbatim_phrase`, `example`, `reasoning`, `conditions` (prose JSON list — explicitly NOT evaluated by Stage 4), `conflicts_with` (JSON list of ids), `confidence ∈ {stated,repeated,exemplified}`, `source_session`, `source_turn`, `keywords` (JSON), `embedded_at` (null until Job 11), timestamps. Indexes on domain, keywords, (session,turn), and a partial index on `embedded_at IS NULL`.
- **`coverage.sql`** — `(domain, sub_topic)` UNIQUE; `probe_count`, `items_captured`, `gap_score` (1.0=untouched, 0.0=saturated), `last_probed`, `last_probed_version`, `library_version_seen`, `turns_since_last_capture`.
- **`vocabulary.sql`** — normalized phrase as PK, `verbatim_examples` JSON, `type ∈ {np,vp,idiom,metaphor,stock_expression}`, `frequency`, `first_seen` (captured_items.id), `last_seen`, `domains` JSON, `aliases` JSON.
- **`clarification_queue.sql`** — `session_id`, `captured_id_a`, `captured_id_b`, `suggested_probe`, `status ∈ {queued,asked,resolved,dismissed}`, `resolution_id` (captured_items id of the resolving rule). Index on `(session_id, status)`.
- **`sessions.sql`** — `session_id` PK (format `onboarding-NN-YYYY-MM-DD[-N]`), `type ∈ {onboarding,mentoring}`, `domain`, `status ∈ {active,complete,abandoned}`, timestamps, `turns`, `abandoned_reason`, `notes`.

### 1.3 Per-domain probe library (read-only at runtime)

`skills/interview/solomon-interview-engine/probe_library/<domain>.yaml`, one file per domain (industry, belief-system, why, principles, ideal-outcomes, non-negotiables, scopes, plus mentoring-only ones: pricing, hiring, ops, customer, vendor, finance, plus `_generic.yaml`). Each declares `domain`, `version` (semver), `priority` (1–10), and:

- **`probe_style`** — the seven canonical mirroring rules (verbatim block, copied into every domain file so each ships standalone). Rules 1–7: use exact words; don't editorialize; build follow-up on echoed phrase; drop filler; short is better; follow emotional content; pivot plainly when topic shifts.
- **`required_fields`** — ordered list of fields the session must fill (Session 0 has 7: business_category, primary_product_or_service, customer_orientation, geographic_scope, revenue_model, growth_stage, concentration_risk). Each has `id`, `prompt`, `accepts`, `satisfied_when`, `follow_up_keywords`. Fields can be filled naturally during discovery OR via direct prompt in Stage C; hard cap of 2 turns per field. *"I don't know" / "not applicable" / "decline" each count as filled.*
- **`keywords`** — clusters with ranked probe templates using `{phrase}` slot. ~12 clusters per domain.
- **`fallbacks`** — generic forward prompts when nothing keys.

Semver bump rules are documented: patch = new templates under existing keywords; minor = new keywords/required_fields; major = breaking schema.

### 1.4 Onboarding-session wrappers (the 7-stage flow)

Each `solomon-onboarding-NN-<domain>/SKILL.md` is a thin orchestrator on top of the engine, but it adds **two structural elements** beyond pure discovery:

- **Five stages**: A. Setup (open or resume `db.sessions` row, set active_domain, version-migration check) → B. Discovery (engine loops until coverage-tracker says complete) → C. Required-fields pass (ordered, with 2-turn cap and field-tag `field:<id>` in `keywords`) → D. Closing checkpoint (read-back via SQL query F2, owner can confirm/correct/add/keep-talking/abandon — five intents with explicit DB writes per intent) → E. Close (status=complete, render `foundation/NN-<domain>.yaml` via SQL query F3).
- **Four canonical SQL queries** (F1–F4) embedded in the SKILL.md using SQLite JSON1: F1 = unfilled required_field ids, F2 = full session capture with `required_field_tag` extracted, F3 = render foundation YAML (three result sets: required-fields latest-wins, discovery captures, top-30 vocabulary samples), F4 = turns-on-field for the 2-turn cap.

### 1.5 ELIZA-listening discipline

Documented in `references/eliza-listening.md` and pinned in `SOUL.md` (auto-loaded every turn). Key points: borrows the interviewer *shape* of ELIZA but **not** the canned response tables. Adds concrete-example forcing, real-time contradiction detection, coverage tracking, confidence scoring, vocabulary capture. Three-way comparative examples (choppy / over-sleek / right) are shipped in the reference for the LLM to internalize.

### 1.6 Data flow per owner turn (Drive)

```
owner_text
  → solomon-redact
  → [parallel] solomon-extraction (Sonnet) ─→ captured_items rows
                                          ─→ coverage updates
                                          ─→ triggers solomon-contradiction-check (Sonnet, per row)
                                                  ─→ conflicts_with updates + clarification_queue rows
              solomon-vocabulary-capture (spaCy + Sonnet) ─→ vocabulary rows
  → solomon-coverage-tracker.read() ─→ session-complete? OR next sub-topic
  → solomon-interview-engine.select_next_probe() ─→ clarification_queue first, else keyword match, else fallback
  → ask one question
```

LLM cost per owner turn: ~3 Sonnet calls (extraction + per-row contradiction check + vocabulary idioms pass). All can run in parallel except contradiction-check, which is gated on extraction's row ids.

---

## 2. What we built (`/root/projects/solomon/`)

A plain script. The honest summary:

- **`solomon/onboarding/session_runner.py`** (174 lines). One function: `run_session(session_key)` reads questions from `curriculum/sessions.yaml`, loops `input()` calls in the terminal, collects `[{q, a}, …]`, then makes **one** Sonnet call at the end of the session with the entire Q&A blob asking it to emit a "structured document" as JSON. Writes `{session, captured_at, document, raw_qa}` to `~/.hermes/solomon/foundation/<name>.yaml`.
- **`solomon/onboarding/curriculum/sessions.yaml`** (89 lines). 6 sessions (1=belief, 2=why, 3=principles, 4=ideal-outcomes, 5=non-negotiables, 6=domain-map). Each is a `name`, `duration_min`, `output_file(s)`, and a flat list of `questions:` strings. Session 3 has a one-line `followups:` hint. Session 5 has a `format_note:` for the structurer prompt. No required_fields, no keywords, no probe priorities, no fallbacks.
- **`solomon/conductor.py`**. Does not reference the interview phase at all. `grep "interview|probe|onboard|listen|eliza"` returns nothing. The conductor is the decision-phase pipeline. There is no interview/decision phase boundary in code.
- **No Session 0 (industry)**. The Drive version explicitly treats industry context as the floor everything else builds on; we skip it.
- **No `captured_items` table.** `solomon/storage/schema.sql` has `tenants`, `raw_events`, `decisions`, `heuristics`, `pending_heuristics`, `skills`, `predictions`, `counterfactuals`, `mentoring_sessions`, `audit_log`, `pending_approvals`, `autonomy_state`, `regret_signals`, `fragility_log`, `private_sessions`, `cycle_log`, `embeddings`, `working_memory`, `open_items`, `ingestion_jobs`, `ingestion_documents`, `schema_meta`. The interview tables (`captured_items`, `coverage`, `vocabulary`, `clarification_queue`, `sessions`) are all missing.
- **No vocabulary capture.** No spaCy, no idiom extraction.
- **No coverage tracker.** Sessions end when the question list runs out, full stop. No saturation check, no diminishing-returns check, no resume.
- **No contradiction check.** Conflicts between owner answers within or across sessions go undetected.
- **No confidence scoring.** Every captured fact is implicitly the same weight.
- **No verbatim phrase preservation.** The structurer LLM is free to paraphrase the owner because there is no `verbatim_phrase` column and no rule that the YAML must echo exact wording.
- **No per-turn structured extraction.** Extraction happens once, at end-of-session, on the whole transcript at once — which means contradictions surface to the LLM blob with no targeted probe, examples don't get linked to claims, and `keywords` arrays cannot be populated by a per-claim model.
- **No probe libraries / no `{phrase}` substitution.** The 6 sessions are 5 questions each with no follow-up logic beyond what the model decides to do mid-call.
- **No ELIZA-listening prompt anywhere.** The system prompt in the runner is one line: *"You are Solomon's onboarding assistant. Convert the owner's answers into a clean structured document matching the requested format. Do not invent content the owner did not say."* That's it.
- **Learning / mentoring modules are stubs.** `solomon/learning/__init__.py` and `solomon/mentoring/__init__.py` are empty. So there's no place for cross-session contradictions to land either.

---

## 3. Best-of-both recommendation

### Take from the Drive version (essentially all of the interview subsystem)

- **The five-skill split** (engine / extraction / vocabulary / coverage / contradiction-check). Each runs in <50 LOC of orchestration once helpers exist. The conceptual decomposition is correct and there is no good reason to fuse them.
- **All five lifecycle tables** (`captured_items`, `coverage`, `vocabulary`, `clarification_queue`, `sessions`) — adopt the schemas verbatim. They are well-designed (JSON1 indexes, partial index on `embedded_at IS NULL`, ULID PKs, explicit `CHECK` constraints on enums).
- **Probe libraries.** Per-domain YAMLs with `version`, `probe_style` (the seven rules), `required_fields`, ranked `keywords` clusters with `{phrase}` substitution, and `fallbacks`. The Session-0 (industry) library is already written (`probe_library/industry.yaml`, 201 lines, complete) — port as-is.
- **The seven mirroring rules** as a copy-into-every-probe-library block. Yes, it's duplication; it's intentional duplication so each library file ships standalone.
- **Confidence ladder** (`stated` / `repeated` / `exemplified`). Simple, useful for downstream retrieval ranking, costs nothing.
- **Real-time contradiction check via `clarification_queue`** (NOT `mentoring_queue`). Same-session resolution is the whole point — context is still fresh.
- **Session-complete dual rule** (saturation OR diminishing returns).
- **The five-stage onboarding wrapper pattern** (Setup → Discovery → Required-fields pass → Closing checkpoint → Close-with-YAML-render). The Closing checkpoint with five owner-intent paths (confirm / correct / add / keep-talking / abandon) is genuinely good UX.
- **Foundation YAMLs as derived summaries**, rendered from `captured_items` at session close via canonical SQL (queries F1–F4 per session). DB is source of truth; YAML is a view.
- **Session-0 first** (industry/sector). Belief and principles only make sense relative to industry.
- **ELIZA-listening pin in `SOUL.md`** so it is auto-loaded every interview turn.

### Take from our build

- **`solomon/onboarding/session_runner.py` as the entry-point shell.** The CLI plumbing (`solomon onboard session_N`, the curriculum loader, the `~/.hermes/solomon/foundation/` write target) is the right shape — we just need to swap the body for the engine loop.
- **Our storage pool / tenant model** (`storage/pool.py`, `get_or_create_tenant_id`). The Drive version assumes a single-tenant SQLite, but we already have multi-tenant scoping. Keep ours, add the interview tables to it.
- **Our reasoning client abstraction** (`reasoning.llm.get_client`, with `tier` routing). The Drive SKILL.mds say "Sonnet" by name; we should route via tier ("extraction", "contradiction", "vocabulary-idioms") so the model is swappable.
- **Our Hermes adapter / Conductor seam.** The Drive version assumes Hermes skills register themselves at gateway load. We have a `plugin.py` + `conductor.py` that already integrate cleanly; the interview engine should plug in there as a separate phase, not as 27 skill files.

### Cut from both

- **From the Drive version**: the 8 onboarding SKILL.md files as Hermes skills. We don't need 8 markdown files per session — we need 1 Python session-runner that reads probe libraries + session config. The skill-pack packaging is Drive-specific.
- **From the Drive version**: probe-library version migration to `mentoring_queue` priority 7. Keep `library_version_seen` on `coverage` for diagnostics, but defer the auto-mentoring-queue write to v2 — we don't have a mentoring loop yet.
- **From ours**: the end-of-session "ask the LLM to structure all answers" pattern. It's an anti-pattern relative to per-turn extraction; drop it entirely.
- **From ours**: the `format_note:` field on session_5. Replaced by `required_fields` in the probe library.

---

## 4. Concrete integration plan

Adopt the Drive interview architecture inside our `/root/projects/solomon/` codebase. Listed in dependency order. Roughly 2–3 days of work for a single contributor.

### 4.1 Schema (Day 1 morning)

Append to `solomon/storage/schema.sql` (or create `solomon/storage/schema_interview.sql` and load both):

- `captured_items` — copy from `db/schemas/captured_items.sql`. Tenant-scope: add `tenant_id` to PK or as an indexed column to match our multi-tenant model.
- `coverage` — copy from `db/schemas/coverage.sql`. Add `tenant_id`.
- `vocabulary` — copy from `db/schemas/vocabulary.sql`. PK becomes `(tenant_id, phrase)`.
- `clarification_queue` — copy from `db/schemas/clarification_queue.sql`. Add `tenant_id`.
- `sessions` — copy from `db/schemas/sessions.sql`. Add `tenant_id`.

Bump `schema_meta` version.

### 4.2 Probe libraries (Day 1 afternoon)

Create `solomon/onboarding/probe_library/`:

- Port `industry.yaml` from Drive verbatim (201 lines, complete).
- Stub the other six: `belief_system.yaml`, `why.yaml`, `principles.yaml`, `ideal_outcomes.yaml`, `non_negotiables.yaml`, `scopes.yaml`. Each gets `domain`, `version: 0.1.0`, the `probe_style` block (copy-paste of the seven rules), an empty `required_fields:` list (fill in v1.1), 6–10 `keywords:` clusters with 2–3 templates each, and 5 `fallbacks:`.
- `_generic.yaml` for cross-domain fallbacks.

Plus a `README.md` in that dir documenting the semver bump rules.

### 4.3 Interview engine (Day 1 evening — Day 2 morning)

Create `solomon/onboarding/interview/`:

- **`engine.py`** — `select_next_probe(session_id, domain, last_answer) -> str`. Implements the 8-step process from `solomon-interview-engine/SKILL.md`. Pure DB + YAML reads + verbatim string formatting, no LLM call.
- **`extraction.py`** — `extract(session_id, turn, owner_text) -> List[captured_items_row]`. One Sonnet call via `reasoning.llm.get_client(tier="extraction")`. JSON-mode, schema matches `captured_items`. Writes rows, updates coverage. Returns row ids so the caller can fan out to contradiction-check.
- **`vocabulary.py`** — `capture(owner_text, source_item_id) -> List[vocabulary_row]`. Pass 1: spaCy NP/VP chunks (add `spacy` to `pyproject.toml`, ship `en_core_web_sm` install in `install.sh`). Pass 2: Sonnet idiom/metaphor extraction (`tier="vocabulary"`, ~200 tokens out). Normalize per the rule in `vocabulary.sql`. Upsert with frequency increment.
- **`coverage.py`** — `next_sub_topic(domain) -> Optional[str]`, `is_session_complete(session_id, domain) -> bool` (implementing the saturation OR diminishing-returns rule). Pure SQL.
- **`contradiction.py`** — `check(new_item_id) -> List[clarification_row]`. Queries existing `captured_items WHERE domain=?`, runs Sonnet pairwise (`tier="contradiction"`), writes `conflicts_with` + `clarification_queue`.
- **`redact.py`** — port `solomon-redact` PII pass; or wire to whatever PII pass we already have in `ingestion/sensitivity_filter.py`.

### 4.4 Session runner (Day 2 afternoon)

Rewrite `solomon/onboarding/session_runner.py`:

- Keep the CLI shell (`solomon onboard session_N`) and the foundation-dir write target.
- Replace `run_session()` body with the five-stage flow from `solomon-onboarding-00-industry/SKILL.md`:
  - Stage A: open/resume `sessions` row.
  - Stage B: loop `engine.select_next_probe()` → print → `input()` → run redact + extraction + vocabulary (parallel) + contradiction-check until `coverage.is_session_complete()` is true.
  - Stage C: F1-style query for unfilled required_fields → ask each in order, 2-turn cap, tag captured rows with `field:<id>` in `keywords`.
  - Stage D: F2-style read-back, parse owner intent via LLM classifier into {confirm, correct, add, keep_talking, abandon}, dispatch accordingly. Loop until confirm.
  - Stage E: F3 rendering of `foundation/NN-<domain>.yaml` (three SQL result sets + pyyaml dump). Set `sessions.status='complete'`.
- Add `solomon onboard status` command — implements the `solomon-onboarding-status` SKILL.md logic over our DB.

Delete `curriculum/sessions.yaml` — superseded by probe libraries.

### 4.5 SOUL / system-prompt wiring (Day 2 evening)

- Pin the ELIZA-listening rule (paragraph from `SOUL.md` §"ELIZA listening rule") into our system prompt for any LLM call where the request originates inside a `phase=interview` flow.
- Add a `phase` flag to whatever we pass into `reasoning.llm.get_client().call(...)` so the prompt assembler can decide.
- Ship `references/eliza-listening.md` (port verbatim) into our repo's `docs/` for the LLM to consult via skill-load if/when we adopt Hermes skill packaging.

### 4.6 Data flow per turn (post-integration)

```
solomon onboard session_0
  → engine.select_next_probe()           # SQL + YAML, no LLM
  → print(probe); answer = input()
  → redact(answer)
  → parallel:
      ext_rows = extraction.extract(...)        # Sonnet
      voc_rows = vocabulary.capture(...)        # spaCy + Sonnet
  → for r in ext_rows:
      contradiction.check(r.id)                 # Sonnet, only existing same-domain rows
  → coverage.refresh(session_id, domain)
  → if coverage.is_session_complete(): break
  → loop
  → required_fields pass
  → closing checkpoint
  → render foundation/00-industry.yaml
```

LLM budget per owner turn: ~3 Sonnet calls (extraction, contradiction, vocabulary-idioms). Tunable downward: idiom pass can drop to once-per-3-turns batched; contradiction-check can short-circuit when `captured_items` for the domain is empty.

### 4.7 Tests to add

- Unit: `select_next_probe()` respects clarification_queue, keyword priority, saturation, fallbacks.
- Unit: extraction returns valid rows for a known-good owner text fixture.
- Unit: vocabulary normalization (lowercase / article strip / hyphen preserve / no stemming) round-trips.
- Unit: coverage `gap_score` math.
- Integration: full session_0 run with mocked LLM returns deterministic captured_items + a populated `foundation/00-industry.yaml`.
- Phase guard: assert no `phase: interview` Python module is reachable from `conductor.py` import graph.

---

## 5. Bottom line

The Drive interview subsystem is the one part of that codebase that is unambiguously more mature than ours. It has been thought about carefully (the seven mirroring rules, the three-way example library, the required_fields pass, the clarification_queue vs mentoring_queue distinction, the YAML-as-derived-summary discipline). What we built is a Q&A loop with one structuring LLM call.

The integration is mechanical — five new tables, six probe libraries, ~600 lines of Python across six modules, and a rewrite of `session_runner.py`'s body. Two to three engineer-days. The payoff is that everything downstream of onboarding (mentoring, decision-phase retrieval, vocabulary-respecting voice on outputs, hard-rule promotion of `captured_items.conditions` into `foundation/05-non-negotiables.yaml`) gets a real foundation to stand on instead of free-form YAML blobs.

Recommendation: do it before any more decision-phase work lands.

# REPORT — Corpus Ingestion / Memory / Storage Subsystem

Comparison of the **Drive version** of Solomon (the one we discovered at `/root/projects/solomon-from-drive/`, marked "Tier 0 — Done" in `BUILD-STATUS.md`) against the **parallel version we built** at `/root/projects/solomon/`. Goal: keep everything **local and self-contained** (user preference), and adopt the best ideas from both.

---

## 1. What the Drive version has

The Drive version is materially more complete on this subsystem. It treats the corpus as a first-class three-layer model (raw / wiki / index+log) and wraps it in a hardened watcher + worker pipeline. Specifics:

### 1.1 Architecture: 4 Pinecone namespaces with weighted retrieval

`corpus_ingest/config.py` defines four canonical namespaces and the **Lane-1 weight vector** that re-ranks across them:

| Namespace | Role | Weight |
|---|---|---|
| `solomon-corpus-wiki` | LLM-synthesized pages (entities, concepts, playbooks) | **0.40** (highest) |
| `solomon-captured-items` | owner's stated rules from interview phase | 0.30 |
| `solomon-corpus-raw` | grounding/citation snippets | 0.20 |
| `solomon-decision-log` | historical decisions | 0.10 |

This embodies the **Karpathy LLM-Wiki insight**: synthesized knowledge outranks raw text 2:1 because raw fragments don't carry the structure the wiki maintainer has imposed. The weights sum to 1.0, are per-query overridable, and live in `LANE1_WEIGHTS`.

### 1.2 Hybrid file-type extraction (`corpus_ingest/extract.py`, 240 lines)

A dispatch table covers 11 extension buckets, each with a lazy-imported optional dependency so the module loads even when libs are missing:

- `.txt/.md` → plain
- `.rtf` → `striprtf`
- `.pdf` → `pypdf` text layer; **falls back to Sonnet multimodal** when text layer is empty (scanned PDFs)
- `.docx` → `python-docx` (paragraphs **and** tables)
- `.pptx` → `python-pptx`, slide-by-slide with `## Slide N` headers
- `.xlsx` → `openpyxl` read-only, sheet-by-sheet, pipe-joined rows
- `.html/.htm` → custom `HTMLParser` stripper (skips `<script>/<style>`)
- `.eml/.mbox` → `email.parser.BytesParser`, headers + first text/plain part (HTML fallback)
- `.csv/.tsv` → pipe-joined rows
- `.json` → pretty-printed, sorted keys
- `.png/.jpg/.jpeg/.heic` → **Sonnet multimodal** via `llm.extract_text_via_sonnet`; HEIC converted in-memory through `pillow-heif`

Missing deps raise `UnsupportedFileType` → parked into `corpus/inbox/_unsupported/` rather than crashing. Audio is parked until whisper.cpp wiring lands.

### 1.3 Karpathy LLM-Wiki two-pass pipeline (`corpus_ingest/llm_passes.py`)

Per-file flow:

1. **Pass 1 — Extract** (one Sonnet call, `prompts.py::EXTRACT_SYSTEM`): returns a JSON envelope `{summary, entities, concepts, playbooks, proposed_rules}` with normalized slugs and `new_info` paragraphs.
2. **Pass 2 — Page merge** (one Sonnet call per touched wiki page): reads the existing `corpus/wiki/{entities,concepts,playbooks}/<slug>.md`, merges `new_info` into the canonical section structure, appends to `## Sources`, updates `last_updated`, and returns the full new markdown.

The wiki page conventions are enforced inline in the prompt (entity pages have Identity / Relationship history / Key rules / Open threads / Cross-refs / Sources; concept pages have Definition / Owner's stated rule / Exceptions / ...; playbook pages have Trigger / Steps / Inputs/outputs / Failure modes / ...).

### 1.4 Section-hash wiki vector cleanup (`corpus_ingest/wiki.py`)

Each `## Heading` section of a page becomes one Pinecone vector with id `wiki:<slug>:<section_hash>`. The `db.wiki_vectors` SQLite table stores the live hash list per page. On re-embed: diff old vs new hashes, **delete orphaned vectors** for sections that disappeared, embed only sections that are new. This makes wiki updates *idempotent and incremental* instead of full-page rewrites.

### 1.5 `proposed_rules` → `mentoring_queue` flow (`corpus_ingest/rules.py`)

When Pass 1 detects FIRST-PERSON owner rules buried in SOPs/emails ("we never quote below cost+15%", "our rule is..."), they're written as `db.proposed_rules` rows with `status='queued'` AND a paired `db.mentoring_queue` row (`source='corpus_rule_proposal'`, priority 4). The owner confirms during the next mentoring session — at which point they get promoted to `captured_items`. Deduped by `(source_path, verbatim_excerpt)`.

This is the **bridge that lets bulk material teach Solomon the same rules the interview engine extracts** — without auto-trusting the LLM.

### 1.6 SHA256 manifest + idempotency (`corpus_ingest/manifest.py` + `db/schemas/ingested_files.sql`)

Every file is SHA256-d up front. A `db.ingested_files` row tracks `(id, sha256 UNIQUE, status, category, raw_path, pinecone_vectors, wiki_pages_touched, error_message)`. Statuses: `pending → in_progress → success | partial | failed | forgotten`. Re-ingesting the same content is a no-op.

### 1.7 Inbox watcher worker (`workers/corpus-inbox-watcher/__main__.py`, 217 lines)

A long-lived OS-supervised Python process that:

- Uses **`watchdog`** for recursive FS events under `corpus/inbox/`
- **Debounces** for 30s after the last event, capped at 5 resets OR 5 min total (livelock prevention)
- **File-stable check**: confirms size unchanged for 3s before queueing (avoids picking up half-written files)
- **Catch-up scan** on startup: any pre-existing inbox files get queued (covers crash-mid-debounce)
- Writes `db.events(source='file_dropped', payload={path})` rows; `pipeline-tick` picks them up
- Falls back to 5s polling if `watchdog` isn't installed
- Skips `_oversized/`, `_unsupported/`, `_pre-redaction/` parking dirs

Owner drops a file; ingest fires within ~30s.

### 1.8 Plaud IMAP IDLE worker (`workers/plaud-ingest/__main__.py`, 227 lines)

Plaud (voice recorder) emails transcripts via AutoFlow. The worker runs **two threads**:

1. **IMAP IDLE listener** (`imapclient`): instant push notification of new mail, max 29-min refresh cycle
2. **60s backup poller** (stdlib `imaplib`): catches anything IDLE misses

Persistent state in `db.plaud_state`: `last_seen_uid`, 7-day `recent_email_ids` ring buffer (capped 2000), `last_idle_at`, `last_poll_at`, `consecutive_fails`. Three consecutive failures → Telegram alert. Saves `.txt` attachments to `corpus/inbox/messages/` with ISO timestamp prefix; the inbox-watcher then picks them up.

### 1.9 Memory provider Hermes plugin (`hermes-plugins/solomon-memory-provider/__init__.py`)

Registers a `prefetch` hook. On every turn:
1. Extract user message
2. Embed via OpenAI `text-embedding-3-large` (3072 dims)
3. Query all 4 namespaces in parallel (`PER_NAMESPACE_FETCH=6` over-fetch per namespace)
4. Multiply each match's `score * LANE1_WEIGHTS[namespace]`
5. Global re-rank, return top 8 with citation paths (`raw_path:chunk_idx`, `wiki_path`, `captured_items#<id>`, `decisions/log.md#<anchor>`)

Failures return `[]` — turn continues without context rather than blocking.

### 1.10 Redaction layer (`skills/utilities/solomon-redact/redactor.py`, 168 lines)

Phase-agnostic utility, called by ingest (decision phase) AND interview-phase skills. Two layers:
1. **spaCy NER** for PERSON/ORG/LOC/GPE → `[REDACTED:entity]`, with owner allowlist (`corpus/schema.md::entity_allowlist`)
2. **Regex patterns**: SSN, phone, credit card (with Luhn check), AWS access keys (`AKIA...`), prefixed API keys (`Bearer ...`, `api_key=...`), SSH PEM markers, labeled passwords

Quarantine path: originals → `corpus/raw/_pre-redaction/<sha256>.bin` (AES-256-GCM with the BIP-39 backup key — `install.sh` provisions the key). Audit log: kind + offset, **never the value**.

### 1.11 Owner-editable schema (`corpus/schema.md`)

A single markdown file with embedded YAML blocks controls: routing extension map, file size limits, oversized/unsupported parking paths, salience threshold, redaction skip globs, entity allowlist, transcription backend (whisper.cpp local OR API), OCR backend, wiki orphan grace days, vocabulary normalization. `corpus_ingest/config.py::load_schema()` parses on every run, so the owner edits one place.

### 1.12 Lint job, forget cascade, sleep cycle

- `solomon-corpus-lint` (Sleep-Cycle Job 9): contradictions, stale pages, orphan pages, missing cross-refs, near-duplicate detection (cosine > 0.95 in raw namespace), parking-folder check. Top hits go to `mentoring_queue`.
- `solomon-corpus-forget`: cascading GDPR-style deletion — entity page → hard delete; concept/playbook pages → LLM-driven rewrite (Opus); raw files → encrypted move to `corpus/_forgotten/<sha256>/`; `captured_items` rows → redact-or-drop depending on whether the rule survives.
- Sleep-Cycle Job 11 (`embed-pending`): embeds new `captured_items` to `solomon-captured-items` namespace. Job 12 (`yaml-reconcile`) keeps foundation YAMLs in sync.

---

## 2. What we built (in `/root/projects/solomon/solomon/ingestion/`)

A simpler, more orthogonal pipeline. Scope is smaller, but several pieces are conceptually cleaner. ~2,400 lines across 10 modules + a generous Postgres schema.

### 2.1 Pipeline shape (`ingestion/upload_handler.py`)

Per-document stages:
1. Read text from disk (no per-extension extractor — only handles UTF-8 text files)
2. `sensitivity_filter.scrub()` — regex PII redaction (SSN, SIN, CC, phone, passport, email). No NER.
3. `classifier.classify_document()` — **one LLM call returns `{document_type, period_start/end, participants, domain, salience_estimate}`**. Falls back to filename heuristics if no LLM. Types: `email_thread, proposal, transcript, contract, sop, feedback, internal_doc, text_exchange, note, spreadsheet, other`.
4. `chunker.chunk_document(text, type)` — **type-aware chunking**: email-thread splits on quoted-reply boundaries; transcript splits on speaker turns (with short-turn merging); contract/SOP/internal-doc splits on headings; everything else is generic paragraph chunking with 1500-char target + 100-char overlap.
5. `budget_tracker.can_spend()` gate — per-tenant monthly token cap (~$50 default at $5/1M)
6. `embedder.embed_batch()` — **default: local `sentence-transformers/all-MiniLM-L6-v2`, 384 dims, CPU, no network**. Opt-in `SOLOMON_EMBEDDING_PROVIDER=openai` switches to `text-embedding-3-small` (1536 dims).
7. `extractor.extract_from_chunk()` — Sonnet/Opus deep LLM call returns `ExtractedDecision{situation, options_considered, decision, reasoning, outcome, decision_maker, timestamp, confidence}`. Gated by salience ≥ 0.3 and decision-keyword pre-filter. Stored as a historical row in `decisions` (the same table that holds real-time decisions, with `historical=true`).

Batch-level passes after all docs:
- `heuristic_miner.mine_batch()` — groups historical decisions by scope, asks deep LLM for **recurring patterns** (≥5 decisions). Outputs `pending_heuristics` rows.
- `cross_referencer.find_references()` — pure-Python (no LLM): normalized email subjects (`Re:`/`Fwd:` stripped), filename-similarity ≥0.80 SequenceMatcher

### 2.2 Storage (`solomon/storage/schema.sql`, 350 lines)

**Postgres with `pgvector` and `pg_trgm` extensions.** Single `embeddings` table for everything:

```sql
CREATE TABLE embeddings (
  embedding_id BIGSERIAL PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  source_table TEXT NOT NULL,   -- 'decisions' | 'captured_items' | 'ingestion_chunk' | ...
  source_id    BIGINT NOT NULL,
  vector       vector(384),     -- HNSW index, cosine_ops
  created_at   TIMESTAMPTZ
);
```

Other tables: `tenants, raw_events, decisions, heuristics, pending_heuristics, skills, predictions, counterfactuals, mentoring_sessions, audit_log, pending_approvals, autonomy_state, regret_signals, fragility_log, private_sessions, cycle_log, working_memory, open_items, ingestion_jobs, ingestion_documents`.

### 2.3 Memory layer (`solomon/memory/{working,retrieval}.py`)

- **`working.py`** — Postgres-backed working memory (no Redis), 7-day TTL (14 in vacation mode), 50-item soft cap, lowest-salience-oldest eviction. Keyed `scope:<scope>:<event_id>`.
- **`retrieval.py`** — Multi-lane retrieval with **5 lanes**: `semantic, recency, entity, pressure, foundation`. Weights `0.30/0.20/0.25/0.15/0.10`. Lane-merge with exponential age decay (0.02/day), top-12 final. **Semantic + Pressure are stubbed** today; Recency/Entity/Foundation work. Foundation reads `~/.hermes/solomon/foundation/{principles,non_negotiables}.yaml`.

### 2.4 What we DON'T have

- No file watcher / no auto-trigger — ingestion is invoked manually via `solomon ingestion` CLI
- No wiki layer (no entity/concept/playbook pages)
- No `proposed_rules` → mentoring flow (heuristics go straight to `pending_heuristics`)
- No multimodal Sonnet path for PDFs/images — we only read UTF-8 text files
- No per-extension extractors (no pypdf / python-docx / pptx / xlsx / html / eml / csv)
- No Plaud / IMAP IDLE worker
- No section-hash wiki vector cleanup
- No spaCy NER redaction (regex-only)
- No SHA256 manifest / dedup — re-running ingestion re-processes everything
- No `corpus_ingest_lint` job
- No forget-cascade
- No quarantine-on-redaction

---

## 3. Best-of-both recommendation

The user wants **LOCAL EVERYTHING**, so the Drive version's Pinecone dependency is non-negotiable-to-replace. But almost everything else from the Drive version is worth porting. The verdict:

### Keep from our version
- **pgvector** as the vector store (single Postgres instance, no external service)
- **`sentence-transformers` MiniLM-L6-v2** local embeddings (384 dims, free, fast, no API key)
- **Type-aware chunker** (email/transcript/contract/SOP) — strictly better than the Drive's single 800-token sliding window
- **`classifier.py`** one-shot type+salience LLM call (the Drive version doesn't have this; routing is purely subfolder/extension-based)
- **`heuristic_miner.py`** cross-document pattern detection — this is a Solomon-level signal the Drive version doesn't implement
- **`cross_referencer.py`** for email-thread + filename-version linking
- **`budget_tracker.py`** per-tenant token cap (the Drive version has no budget guard)
- **5-lane retrieval** (`semantic, recency, entity, pressure, foundation`) — richer than the Drive's single weighted-namespace lane
- **Multi-tenant `tenant_id`-keyed schema** — Drive version is single-tenant
- **Postgres-backed working memory** + open_items + mentoring_sessions tables

### Port from the Drive version
- **Karpathy LLM-Wiki two-pass pattern**: extract envelope → page-merge. This is the highest-leverage idea in the Drive codebase. It turns raw documents into a *structured, citable, owner-readable* knowledge base. We currently throw raw text into pgvector and extract decisions; we miss the entity/concept/playbook synthesis layer entirely.
- **`proposed_rules` → `mentoring_queue` flow**: bulk material discovers rules, owner confirms before promotion to `captured_items` (or `heuristics` in our schema). Critical bridge between corpus + interview phases.
- **`corpus-inbox-watcher` worker**: 217 lines, watchdog + debounce + file-stable + catch-up. Port as-is, swap SQLite for Postgres.
- **Hybrid file-type extraction** (`extract.py`): port the whole dispatch table. For multimodal PDF/image fallback, **swap Sonnet for a local model** — Ollama + LLaVA or Llama-3.2-Vision would keep things local. If user accepts a Sonnet-only path for that one fallback, document it as the lone API dependency.
- **Plaud IMAP IDLE worker**: 227 lines, IMAP IDLE + 60s backup poller, dedup ring buffer. Critical for voice-recording ingestion.
- **Redactor** (`redactor.py`): spaCy NER + regex + allowlist + quarantine. Strictly stronger than our regex-only `sensitivity_filter`. **Keep our SensitivityResult interface** but swap the implementation.
- **`corpus/schema.md`** owner-editable YAML config — port the format and the `_yaml_blocks()` parser
- **SHA256 manifest** — add an `ingested_files` table (Postgres) keyed by sha256 with status enum
- **Wiki section-hash cleanup** — once wiki pages exist, port the diff-and-delete vector logic
- **3-layer corpus folder structure** (`corpus/{inbox,raw,wiki}/<category>/`) — port as a directory convention
- **corpus-lint job** — port the contradiction/stale/orphan/near-dup detector
- **forget-cascade** — port the deterministic cascade rules

### Drop entirely (Pinecone-isms)
- `pinecone_client.py` — replaced by pgvector queries
- 4-namespace logic mapped 1:1 to a single `embeddings.source_table` column. Namespaces become a discriminator column:
  - `solomon-corpus-wiki` → `source_table='corpus_wiki'`
  - `solomon-captured-items` → `source_table='captured_items'`
  - `solomon-corpus-raw` → `source_table='corpus_raw'`
  - `solomon-decision-log` → `source_table='decisions'`
- `LANE1_WEIGHTS` survives intact — apply as `score * weight` after a `WHERE source_table IN (...)` query split into 4 parallel queries (cheap on the same Postgres connection).
- The `solomon-memory-provider` Hermes plugin becomes optional — our `MultiLaneRetrieval.retrieve()` already covers the same job inside the conductor. If we want a Hermes-native prefetch hook later, write a thin wrapper around `retrieval.py`.

---

## 4. Concrete integration plan

### 4.1 New module layout in `/root/projects/solomon/solomon/`

```
solomon/
├── ingestion/                 (existing — keep)
│   ├── classifier.py          ✓ keep
│   ├── chunker.py             ✓ keep (type-aware)
│   ├── embedder.py            ✓ keep (local sentence-transformers)
│   ├── extractor.py           ✓ keep (decision extraction)
│   ├── heuristic_miner.py     ✓ keep
│   ├── cross_referencer.py    ✓ keep
│   ├── budget_tracker.py      ✓ keep
│   ├── review_queue.py        ✓ keep
│   ├── upload_handler.py      ⚙ modify — call new corpus_wiki passes
│   └── sensitivity_filter.py  ⚙ replace impl with spaCy+regex+allowlist port
│
├── corpus/                    🆕 NEW package — Drive-style wiki + raw
│   ├── __init__.py
│   ├── route.py               🆕 port from corpus_ingest/route.py
│   ├── extract.py             🆕 port from corpus_ingest/extract.py (drop Sonnet, sub local vision)
│   ├── llm_passes.py          🆕 port from corpus_ingest/llm_passes.py (sub Anthropic for our llm client)
│   ├── prompts.py             🆕 port from corpus_ingest/prompts.py verbatim
│   ├── wiki.py                🆕 port from corpus_ingest/wiki.py — Pinecone calls → pgvector
│   ├── rules.py               🆕 port from corpus_ingest/rules.py — proposed_rules + mentoring_queue
│   ├── manifest.py            🆕 port from corpus_ingest/manifest.py — sha256-keyed ingested_files
│   ├── schema_config.py       🆕 port from corpus_ingest/config.py — parse corpus/schema.md
│   ├── lint.py                🆕 port skills/corpus/solomon-corpus-lint logic
│   └── forget.py              🆕 port skills/corpus/solomon-corpus-forget cascade
│
├── memory/                    (existing)
│   ├── working.py             ✓ keep
│   └── retrieval.py           ⚙ modify — add "wiki" lane + namespace-weight re-ranker
│
├── storage/
│   ├── schema.sql             ⚙ extend — add ingested_files, proposed_rules,
│   │                                      mentoring_queue, wiki_vectors, plaud_state
│   └── corpus_pgvector.py     🆕 NEW — namespace-style query helper
│
├── workers/                   🆕 NEW — long-lived OS-supervised processes
│   ├── corpus_inbox_watcher/__main__.py    🆕 port verbatim, swap sqlite→postgres
│   └── plaud_ingest/__main__.py            🆕 port verbatim, swap sqlite→postgres
│
└── utilities/
    └── redact.py              🆕 port from skills/utilities/solomon-redact/redactor.py
```

### 4.2 New tables to add to `solomon/storage/schema.sql`

```sql
-- File-level manifest (port of db/schemas/ingested_files.sql)
CREATE TABLE ingested_files (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  sha256 TEXT NOT NULL UNIQUE,
  inbox_path_at_ingest TEXT NOT NULL,
  raw_path TEXT,
  size_bytes BIGINT NOT NULL,
  category TEXT NOT NULL CHECK (category IN ('sops','emails','messages','docs','data')),
  status TEXT NOT NULL CHECK (status IN ('pending','in_progress','success','partial','failed','forgotten')),
  pinecone_vectors INTEGER,           -- rename to vector_count later
  wiki_pages_touched JSONB,
  error_message TEXT,
  ingested_at TIMESTAMPTZ NOT NULL
);

-- Rule proposals from corpus extraction
CREATE TABLE proposed_rules (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  domain TEXT NOT NULL,
  proposed_statement TEXT NOT NULL,
  verbatim_excerpt TEXT NOT NULL,
  source_path TEXT NOT NULL,
  keywords JSONB,
  confidence_hint TEXT CHECK (confidence_hint IN ('stated','repeated','exemplified')),
  status TEXT NOT NULL DEFAULT 'queued',
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (source_path, verbatim_excerpt)
);

-- Owner queue (corpus_rule_proposal source mixes with other sources)
CREATE TABLE mentoring_queue (
  id BIGSERIAL PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  source TEXT NOT NULL,                -- 'corpus_rule_proposal' | 'contradiction' | ...
  surfaced_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  priority INT NOT NULL DEFAULT 5,
  payload JSONB NOT NULL
);

-- Wiki section-hash tracking
CREATE TABLE wiki_vectors (
  page_path TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  section_hashes JSONB NOT NULL,
  last_updated TIMESTAMPTZ NOT NULL
);

-- Plaud worker state
CREATE TABLE plaud_state (
  id INT PRIMARY KEY DEFAULT 1,
  tenant_id TEXT NOT NULL,
  last_seen_uid BIGINT,
  recent_email_ids JSONB,
  last_idle_at TIMESTAMPTZ,
  last_poll_at TIMESTAMPTZ,
  consecutive_fails INT NOT NULL DEFAULT 0
);
```

### 4.3 Pinecone → pgvector translation

Keep the four logical namespaces as a `source_table` column on `embeddings`. The Drive's `pc_query(namespace, vector, top_k)` becomes:

```sql
SELECT source_id, vector <=> %s AS distance, ...
FROM embeddings
WHERE tenant_id = %s AND source_table = %s
ORDER BY vector <=> %s
LIMIT %s;
```

`LANE1_WEIGHTS` survives byte-for-byte in `solomon/corpus/__init__.py`:

```python
NAMESPACE_WEIGHTS = {
    "corpus_wiki":     0.40,
    "captured_items":  0.30,
    "corpus_raw":      0.20,
    "decisions":       0.10,
}
```

The memory provider's `prefetch_handler` re-rank logic ports verbatim, just swap `pc_query` for the pgvector helper.

### 4.4 Data flow (text diagram)

```
        ┌──────────────────┐
        │ owner drops file │
        │  corpus/inbox/   │
        └────────┬─────────┘
                 │ FS event
                 ▼
        ┌─────────────────────────┐
        │ corpus-inbox-watcher    │  (port from Drive, sqlite→pg)
        │  watchdog + 30s debounce│
        │  3s file-stable check   │
        │  catch-up scan on boot  │
        └─────────┬───────────────┘
                  │ INSERT raw_events(source='file_dropped')
                  ▼
        ┌─────────────────────────────────────────────────────┐
        │  corpus.ingest.run_for_path()                       │
        │   1. SHA256 → manifest dedup (ingested_files)       │
        │   2. route (subfolder/extension)                    │
        │   3. extract (pypdf/docx/pptx/xlsx/html/eml/csv/    │
        │              json/image — local vision fallback)    │
        │   4. redact (spaCy NER + regex + allowlist)         │
        │   5. quarantine original → _pre-redaction/<sha>.bin │
        │   6. write corpus/raw/<category>/<slug>             │
        │   7. chunk + embed (sentence-transformers, 384d)    │
        │   8. INSERT embeddings (source_table='corpus_raw')  │
        │   9. LLM Pass 1 (extract envelope JSON)             │
        │  10. LLM Pass 2 per wiki page (merge → markdown)    │
        │  11. section-hash diff → delete orphan embeddings,  │
        │      insert new (source_table='corpus_wiki')        │
        │  12. write proposed_rules + mentoring_queue rows    │
        │  13. mark ingested_files.status='success'           │
        └─────────────────────────────────────────────────────┘

        ┌──────────────────┐
        │ Plaud emails .txt│
        └────────┬─────────┘
                 │ IMAP IDLE + 60s poll
                 ▼
        ┌──────────────────────────┐
        │ plaud-ingest worker      │
        │  saves to inbox/messages/│
        └────────┬─────────────────┘
                 │ (file lands → watcher picks up → same pipeline)
                 ▼
              (above)

        ┌──────────────────────────────┐
        │  Decision-time turn          │
        │  conductor.retrieve(event)   │
        └────────┬─────────────────────┘
                 │
                 ▼
        ┌─────────────────────────────────────────────────┐
        │  MultiLaneRetrieval (5 lanes)                   │
        │   semantic lane → 4 parallel pgvector queries   │
        │     across source_table ∈ {wiki, captured_items,│
        │     corpus_raw, decisions}                      │
        │     → weighted re-rank by NAMESPACE_WEIGHTS     │
        │   recency / entity / pressure / foundation lanes│
        │   merge, decay, top-12                          │
        └─────────────────────────────────────────────────┘
```

### 4.5 Ordering of work (3 phases)

**Phase A — Foundations (no LLM changes yet)**
1. Add 5 new tables to `schema.sql`
2. Port `redact.py` from Drive (replace `sensitivity_filter.scrub` impl, keep dataclass interface)
3. Port `extract.py` per-extension dispatch (defer multimodal fallback decision)
4. Port `corpus/schema.md` + `schema_config.py`
5. Port `manifest.py` (sha256 dedup against `ingested_files`)

**Phase B — Wiki layer**
6. Port `prompts.py`, `llm_passes.py`, `wiki.py`, `rules.py` — swap Anthropic SDK calls for our existing `solomon.reasoning.llm.get_client()` (it already supports tiered calls)
7. Update `upload_handler.py` to call Pass 1 + Pass 2 after embed
8. Update `MultiLaneRetrieval` to query the 4 source_tables and weight-rank

**Phase C — Workers**
9. Port `corpus-inbox-watcher` worker (sqlite→psycopg2)
10. Port `plaud-ingest` worker
11. Port `corpus-lint` as a sleep-cycle job
12. Port `corpus-forget` cascade

### 4.6 Local-everything notes

- **Embeddings**: stay on `sentence-transformers` MiniLM-L6-v2 (384d). No OpenAI calls.
- **LLM**: our `solomon.reasoning.llm.get_client()` already abstracts provider; user can plug Ollama or local llama.cpp. The Drive's hard-coded `claude-sonnet-4-6` becomes `client.call(tier='deep', ...)`.
- **Vision fallback for scanned PDFs/images**: two options:
  - **Option 1 (local)**: Ollama + LLaVA-1.6 or Llama-3.2-Vision via the same `get_client(tier='vision')` interface. Keep everything offline.
  - **Option 2 (single API exception)**: keep Sonnet multimodal as the only API dependency, documented and gated behind `SOLOMON_ALLOW_VISION_API=1`. Local PDFs (with text layers) and all non-image formats stay offline.
  - **Recommendation**: ship Option 1, default to skipping image content if no local vision model is configured. Park as `_unsupported` with a clear reason.
- **No Pinecone, no Pinecone index creation in `install.sh`**: drop steps 9 and the `PINECONE_API_KEY` prompt. Provision the pgvector index in the schema migration (already done: `idx_embeddings_vector_hnsw`).
- **Backup-key quarantine**: keep the BIP-39 / Argon2id flow from Drive's `install.sh` for the `_pre-redaction/` AES-256-GCM encryption. Purely local primitive.

---

## 5. Summary

The Drive version is the more mature corpus subsystem. It implements the Karpathy LLM-Wiki pattern end-to-end, has working file/email watchers, a hybrid extraction layer covering 11 file types, a deterministic forget cascade, and a section-hash wiki vector cleanup that no one else seems to have built. Its only sin is hard-coupling to Pinecone — which is exactly what the user wants gone.

Our version is leaner and already local: pgvector, sentence-transformers, multi-tenant, type-aware chunking, cross-document heuristic mining, per-tenant budget caps, 5-lane retrieval. But we lack the wiki layer entirely, our redactor is regex-only, we don't auto-trigger ingest, and we miss the `proposed_rules` bridge that lets bulk material teach Solomon owner-stated rules.

The plan above keeps every local-and-contained guarantee, drops Pinecone, and pulls in the Drive's wiki/watcher/IMAP/redactor/proposed-rules machinery as new modules under `solomon/corpus/` and `solomon/workers/`. Estimated work: ~1,500-2,000 net new lines of Python (most ported, lightly modified), plus 5 schema-table additions, plus one decision on the vision fallback (local LLaVA strongly preferred).

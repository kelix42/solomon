-- Solomon unified schema
-- Default backend: SQLite (single file at ~/.hermes/solomon/solomon.db).
-- Opt-in backend: Postgres (via SOLOMON_DB_URL=postgresql://...).
--
-- This file is written in SQLite dialect with `INTEGER PRIMARY KEY AUTOINCREMENT`
-- where Postgres would use `BIGSERIAL`. SQLite ignores AUTOINCREMENT semantics
-- but treats INTEGER PK as autoincrement. Postgres treats it as a normal int.
-- The Postgres overlay (schema_postgres.sql) handles the swap when needed.
--
-- All tables are tenant-scoped via tenant_id. Single-tenant installs default
-- tenant_id='default'.

-- ===========================================================================
-- Tenant
-- ===========================================================================
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id           TEXT PRIMARY KEY,
    business_name       TEXT NOT NULL,
    timezone            TEXT NOT NULL DEFAULT 'UTC',
    industry            TEXT,
    sub_specialty       TEXT,
    onboarded_at        TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO tenants (tenant_id, business_name) VALUES ('default', 'My Business');

-- ===========================================================================
-- Raw events + Decision events
-- ===========================================================================

-- One row per gateway / capture event. The pipeline reads this.
CREATE TABLE IF NOT EXISTS events (
    event_id            TEXT PRIMARY KEY,        -- ulid
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    source              TEXT NOT NULL,           -- telegram, plaud, gmail, file_dropped, manual, cli, ...
    received_at         TEXT NOT NULL,
    participants        TEXT NOT NULL DEFAULT '[]',   -- JSON
    raw_content         TEXT NOT NULL,
    channel_metadata    TEXT NOT NULL DEFAULT '{}',   -- JSON
    salience_score      REAL,
    classification      TEXT,                    -- JSON {scope, domain, decision_type}
    hard_rule_verdict   TEXT,                    -- 'pass' | 'block' | NULL
    hard_rule_reason    TEXT,
    retrieval_context   TEXT,                    -- JSON
    system1_output      TEXT,
    system2_output      TEXT,
    divergence_score    REAL,
    audit_verdict       TEXT,                    -- approve | downgrade | reject | request_rethink
    audit_reasoning     TEXT,
    owner_state         TEXT,                    -- green | yellow | red | unknown
    owner_state_ceiling INTEGER,                 -- L0..L4 ceiling derived from owner_state (Stage 9)
    effective_autonomy  INTEGER,                 -- min(scope_level, owner_state_ceiling) (Stage 10)
    action_taken        TEXT,
    stage_timings_ms    TEXT NOT NULL DEFAULT '{}',   -- JSON {stage_name: ms}
    status              TEXT NOT NULL DEFAULT 'pending',
                              -- pending | in_progress | complete | skipped | blocked_by_hard_rule | failed
    private             INTEGER NOT NULL DEFAULT 0,   -- bool
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_tenant_received ON events(tenant_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);

-- The H2 decision log mirror. Append-only. One row per completed event.
CREATE TABLE IF NOT EXISTS decisions (
    decision_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id                   TEXT NOT NULL REFERENCES tenants(tenant_id),
    event_id                    TEXT REFERENCES events(event_id),
    scope                       TEXT,
    domain                      TEXT,
    decision_type               TEXT,
    classification_confidence   REAL,
    salience_score              REAL,
    working_memory_used         INTEGER NOT NULL DEFAULT 0,
    retrieval_lanes_used        TEXT NOT NULL DEFAULT '[]',
    heuristics_referenced       TEXT NOT NULL DEFAULT '[]',
    similar_decisions_referenced TEXT NOT NULL DEFAULT '[]',
    foundation_files_used       TEXT NOT NULL DEFAULT '[]',
    system_1_answer             TEXT,
    system_2_answer             TEXT,
    divergence_score            REAL,
    proposed_action             TEXT,
    audit_verdict               TEXT,
    audit_reasoning             TEXT,
    final_action                TEXT,
    autonomy_level_at_time      TEXT,
    owner_action                TEXT,            -- approved | edited | rejected | expired | NULL
    historical                  INTEGER NOT NULL DEFAULT 0,
    created_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_decisions_tenant_created ON decisions(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_scope ON decisions(tenant_id, scope);

-- Legacy alias (some older modules wrote into raw_events). Map to events.
CREATE VIEW IF NOT EXISTS raw_events AS
SELECT event_id, tenant_id, source, received_at, participants, raw_content,
       channel_metadata, salience_score, completed_at AS processed_at, private
FROM events;

-- ===========================================================================
-- Interview phase
-- ===========================================================================

-- Owner-stated rules captured during interview / mentoring sessions.
CREATE TABLE IF NOT EXISTS captured_items (
    id                  TEXT PRIMARY KEY,       -- ulid
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    session_id          TEXT,                   -- FK to sessions; nullable for back-fill
    domain              TEXT NOT NULL,
    type                TEXT NOT NULL,          -- belief | principle | non_negotiable | preference | rule | example | constraint | metric | vocabulary
    statement           TEXT NOT NULL,
    verbatim_phrase     TEXT NOT NULL,
    example             TEXT,
    keywords            TEXT NOT NULL DEFAULT '[]',   -- JSON
    confidence          TEXT NOT NULL DEFAULT 'stated',  -- stated | repeated | exemplified
    conflicts_with      TEXT NOT NULL DEFAULT '[]',   -- JSON list of captured_items.id
    source_session      TEXT,                   -- onboarding-NN, mentoring-YYYYMMDD, etc.
    embedded_at         TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_captured_tenant_domain ON captured_items(tenant_id, domain);
CREATE INDEX IF NOT EXISTS idx_captured_pending_embed ON captured_items(embedded_at) WHERE embedded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_captured_session ON captured_items(session_id);

-- Owner vocabulary (per-tenant phrase frequency).
CREATE TABLE IF NOT EXISTS vocabulary (
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    phrase              TEXT NOT NULL,
    normalised          TEXT NOT NULL,
    kind                TEXT NOT NULL,           -- noun_phrase | verb_phrase | idiom | metaphor | metric
    frequency           INTEGER NOT NULL DEFAULT 1,
    first_seen          TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen           TEXT NOT NULL DEFAULT (datetime('now')),
    example_source_id   TEXT,                    -- a captured_items.id where this first appeared
    PRIMARY KEY (tenant_id, normalised)
);
CREATE INDEX IF NOT EXISTS idx_vocab_freq ON vocabulary(tenant_id, frequency DESC);

-- Coverage tracker per (session, domain, sub_topic).
CREATE TABLE IF NOT EXISTS coverage (
    coverage_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    session_id          TEXT NOT NULL,
    domain              TEXT NOT NULL,
    sub_topic           TEXT NOT NULL,
    probes_asked        INTEGER NOT NULL DEFAULT 0,
    items_captured      INTEGER NOT NULL DEFAULT 0,
    gap_score           REAL NOT NULL DEFAULT 1.0,    -- 1.0 = unprobed, 0.0 = saturated
    turns_since_last_capture INTEGER NOT NULL DEFAULT 0,
    library_version_seen TEXT,
    last_updated        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, session_id, domain, sub_topic)
);
CREATE INDEX IF NOT EXISTS idx_coverage_session ON coverage(tenant_id, session_id);

-- Real-time same-session contradiction queue. The owner resolves these
-- IN the conversation, not later.
CREATE TABLE IF NOT EXISTS clarification_queue (
    clarification_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    session_id          TEXT NOT NULL,
    new_item_id         TEXT NOT NULL REFERENCES captured_items(id),
    conflicting_item_id TEXT NOT NULL REFERENCES captured_items(id),
    reason              TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending | resolved | deferred
    resolution          TEXT,                    -- which side won, or new merged statement
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_clarif_pending ON clarification_queue(tenant_id, status);

-- Onboarding / mentoring sessions.
CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,        -- e.g. onboarding-00-industry
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    domain              TEXT NOT NULL,
    mode                TEXT NOT NULL DEFAULT 'onboarding',   -- onboarding | mentoring | level_up
    status              TEXT NOT NULL DEFAULT 'open',         -- open | complete | abandoned
    library_version     TEXT,
    started_at          TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at        TEXT,
    items_captured      INTEGER NOT NULL DEFAULT 0,
    turn_count          INTEGER NOT NULL DEFAULT 0
);

-- ===========================================================================
-- Heuristics (rules)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS heuristics (
    heuristic_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    scope               TEXT NOT NULL,
    domain              TEXT,
    condition           TEXT NOT NULL,
    action              TEXT NOT NULL,
    reasoning           TEXT,
    confidence          REAL NOT NULL DEFAULT 0.5,
    last_used_at        TEXT,
    last_retrieved_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    source              TEXT NOT NULL,
    provenance          TEXT NOT NULL DEFAULT '{}',
    status              TEXT NOT NULL DEFAULT 'active',  -- active | fragile | archived | superseded
    version             INTEGER NOT NULL DEFAULT 1,
    superseded_by       INTEGER REFERENCES heuristics(heuristic_id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_heur_tenant_scope ON heuristics(tenant_id, scope, status);

-- Pending heuristics waiting on owner approval.
CREATE TABLE IF NOT EXISTS pending_heuristics (
    pending_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    scope               TEXT NOT NULL,
    proposed_condition  TEXT NOT NULL,
    proposed_action     TEXT NOT NULL,
    source              TEXT NOT NULL,           -- ingestion_miner | corpus_rule | surprise_replay
    support_count       INTEGER NOT NULL DEFAULT 1,
    evidence_list       TEXT NOT NULL DEFAULT '[]',
    status              TEXT NOT NULL DEFAULT 'pending',
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Rules buried in corpus material, surfaced for owner review (Drive port).
CREATE TABLE IF NOT EXISTS proposed_rules (
    id                  TEXT PRIMARY KEY,        -- ulid
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    domain              TEXT NOT NULL,
    proposed_statement  TEXT NOT NULL,
    verbatim_excerpt    TEXT NOT NULL,
    source_path         TEXT NOT NULL,
    keywords            TEXT NOT NULL DEFAULT '[]',
    confidence_hint     TEXT,                    -- stated | repeated | exemplified
    status              TEXT NOT NULL DEFAULT 'queued',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source_path, verbatim_excerpt)
);

-- Owner queue (contradictions, rule proposals, drift checks, etc.)
CREATE TABLE IF NOT EXISTS mentoring_queue (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    source              TEXT NOT NULL,           -- corpus_rule_proposal | contradiction | drift | promotion_ready | demotion_alert
    surfaced_at         TEXT NOT NULL DEFAULT (datetime('now')),
    status              TEXT NOT NULL DEFAULT 'queued',
    priority            INTEGER NOT NULL DEFAULT 5,
    payload             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mentq_status ON mentoring_queue(tenant_id, status, priority);

-- ===========================================================================
-- Skills (multi-step playbooks)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS skills (
    skill_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    scope               TEXT NOT NULL,
    domain              TEXT,
    name                TEXT NOT NULL,
    trigger_condition   TEXT,
    steps               TEXT NOT NULL,
    success_criteria    TEXT,
    source_decisions    TEXT NOT NULL DEFAULT '[]',
    confidence          REAL NOT NULL DEFAULT 0.5,
    version             INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===========================================================================
-- Predictions + counterfactuals
-- ===========================================================================
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    decision_id         INTEGER NOT NULL REFERENCES decisions(decision_id),
    prediction_text     TEXT NOT NULL,
    expected_by         TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending | met | missed | partial | unresolved
    actual_outcome      TEXT,
    checked_at          TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pred_pending ON predictions(status, expected_by);

CREATE TABLE IF NOT EXISTS counterfactuals (
    counterfactual_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id                   TEXT NOT NULL REFERENCES tenants(tenant_id),
    decision_id                 INTEGER NOT NULL REFERENCES decisions(decision_id),
    alternative_choice          TEXT NOT NULL,
    predicted_outcome           TEXT,
    evaluated_at                TEXT,
    would_have_been_better      INTEGER,
    created_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===========================================================================
-- Audit + approvals + autonomy
-- ===========================================================================
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    decision_id         INTEGER REFERENCES decisions(decision_id),
    verdict             TEXT NOT NULL,
    reasoning           TEXT,
    model_used          TEXT,
    audited_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pending_approvals (
    approval_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    decision_id         INTEGER NOT NULL REFERENCES decisions(decision_id),
    proposed_action     TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- L0-L4 per scope (Drive's autonomy_state).
CREATE TABLE IF NOT EXISTS scope_autonomy (
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    scope               TEXT NOT NULL,
    level               TEXT NOT NULL DEFAULT 'L0',  -- L0 | L1 | L2 | L3 | L4
    since               TEXT NOT NULL DEFAULT (datetime('now')),
    last_promoted_at    TEXT,
    last_demoted_at     TEXT,
    override_rate_7d    REAL,
    override_rate_30d   REAL,
    PRIMARY KEY (tenant_id, scope)
);

-- Owner-state-modulated ceiling. Stage 9 of the pipeline reads this.
-- v1 default: always 'unknown' which maps to no ceiling (green).
CREATE TABLE IF NOT EXISTS biometrics (
    biometric_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    recorded_at         TEXT NOT NULL DEFAULT (datetime('now')),
    state               TEXT NOT NULL,           -- green | yellow | red | unknown
    source              TEXT,                    -- whoop | manual | derived
    payload             TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_biom_recent ON biometrics(tenant_id, recorded_at DESC);

-- ===========================================================================
-- Sleep cycle bookkeeping
-- ===========================================================================
CREATE TABLE IF NOT EXISTS regret_signals (
    regret_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    decision_id         INTEGER NOT NULL REFERENCES decisions(decision_id),
    heuristic_id        INTEGER REFERENCES heuristics(heuristic_id),
    failure_layer       TEXT,                    -- heuristic | audit_gate | autonomy_level | action_layer
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fragility_log (
    fragility_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    heuristic_id        INTEGER NOT NULL REFERENCES heuristics(heuristic_id),
    mutation            TEXT NOT NULL,
    new_action          TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cycle_log (
    cycle_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    per_job             TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS private_sessions (
    private_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    session_id          TEXT,
    started_at          TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at            TEXT,
    turn_count          INTEGER NOT NULL DEFAULT 0
);

-- ===========================================================================
-- Working memory (the hot cache)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS working_memory (
    wm_key              TEXT NOT NULL,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    payload             TEXT NOT NULL,
    salience            REAL NOT NULL DEFAULT 0.5,
    last_touched_at     TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at          TEXT NOT NULL,
    PRIMARY KEY (tenant_id, wm_key)
);
CREATE INDEX IF NOT EXISTS idx_wm_expires ON working_memory(expires_at);

CREATE TABLE IF NOT EXISTS open_items (
    item_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    scope               TEXT NOT NULL,
    description         TEXT,
    related_decision_id INTEGER REFERENCES decisions(decision_id),
    opened_at           TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at           TEXT
);

-- ===========================================================================
-- Corpus + ingestion (Drive's Karpathy LLM-Wiki pattern + our ingestion)
-- ===========================================================================

-- File-level manifest with sha256 dedup (Drive port).
CREATE TABLE IF NOT EXISTS ingested_files (
    id                  TEXT PRIMARY KEY,        -- ulid
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    sha256              TEXT NOT NULL UNIQUE,
    inbox_path_at_ingest TEXT NOT NULL,
    raw_path            TEXT,
    size_bytes          INTEGER NOT NULL,
    category            TEXT NOT NULL,           -- sops | emails | messages | docs | data
    status              TEXT NOT NULL,           -- pending | in_progress | success | partial | failed | forgotten
    vector_count        INTEGER,
    wiki_pages_touched  TEXT,                    -- JSON list of wiki page paths
    error_message       TEXT,
    ingested_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Ingestion job + per-document manifest (our existing pattern).
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    job_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    status              TEXT NOT NULL DEFAULT 'queued',
    document_count      INTEGER NOT NULL DEFAULT 0,
    started_at          TEXT,
    finished_at         TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ingestion_documents (
    document_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              INTEGER NOT NULL REFERENCES ingestion_jobs(job_id),
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    storage_path        TEXT NOT NULL,
    document_type       TEXT,
    period_start        TEXT,
    period_end          TEXT,
    participants        TEXT NOT NULL DEFAULT '[]',
    domain              TEXT,
    salience_estimate   REAL,
    status              TEXT NOT NULL DEFAULT 'queued',
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Wiki section-hash tracking (Drive's section-hash diff approach).
CREATE TABLE IF NOT EXISTS wiki_vectors (
    page_path           TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    section_hashes      TEXT NOT NULL DEFAULT '{}',
    last_updated        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Plaud IMAP worker state.
CREATE TABLE IF NOT EXISTS plaud_state (
    id                  INTEGER PRIMARY KEY DEFAULT 1,
    tenant_id           TEXT NOT NULL,
    last_seen_uid       INTEGER,
    recent_email_ids    TEXT NOT NULL DEFAULT '[]',
    last_idle_at        TEXT,
    last_poll_at        TEXT,
    consecutive_fails   INTEGER NOT NULL DEFAULT 0
);

-- ===========================================================================
-- Embeddings (vector search)
-- ===========================================================================
-- On SQLite: we store vectors as BLOB (packed float32 array) and use the
-- sqlite-vec extension for nearest-neighbour search.
-- On Postgres: we use pgvector. The Postgres overlay reshapes the column.
--
-- The source_table column is the Drive's "namespace" (corpus_wiki,
-- captured_items, corpus_raw, decisions). The retrieval lane re-ranker
-- applies per-namespace weights after a WHERE source_table IN (...) query.
CREATE TABLE IF NOT EXISTS embeddings (
    embedding_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    source_table        TEXT NOT NULL,           -- 'corpus_wiki' | 'captured_items' | 'corpus_raw' | 'decisions'
    source_id           TEXT NOT NULL,
    vector              BLOB NOT NULL,           -- packed float32 array (SQLite) or vector(384) (PG via overlay)
    metadata            TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, source_table, source_id)
);
CREATE INDEX IF NOT EXISTS idx_emb_lookup ON embeddings(tenant_id, source_table, source_id);

-- ===========================================================================
-- Schema version marker
-- ===========================================================================
CREATE TABLE IF NOT EXISTS schema_meta (
    key                 TEXT PRIMARY KEY,
    value               TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('version', '2');
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('default_tenant_id', 'default');

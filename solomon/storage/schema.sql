-- Solomon storage schema
-- Postgres with pgvector extension. Compatible with local Postgres (Docker)
-- and managed Supabase.
--
-- Every table here corresponds to something in Part 17 of the design doc.
-- Keep this file as the single source of truth for the schema. Migrations
-- live in solomon/storage/migrations/ and are numbered.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- --------------------------------------------------------------------------
-- Tenants
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id           TEXT PRIMARY KEY,
    business_name       TEXT NOT NULL,
    timezone            TEXT NOT NULL DEFAULT 'UTC',
    industry            TEXT,
    sub_specialty       TEXT,
    onboarded_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- --------------------------------------------------------------------------
-- Raw events (Part 2)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_events (
    event_id            TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    source              TEXT NOT NULL,
    received_at         TIMESTAMPTZ NOT NULL,
    participants        JSONB NOT NULL DEFAULT '[]'::jsonb,
    raw_content         TEXT NOT NULL,
    channel_metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    salience_score      NUMERIC(4,3),
    processed_at        TIMESTAMPTZ,
    private             BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_raw_events_tenant_received ON raw_events(tenant_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_events_source ON raw_events(source);

-- --------------------------------------------------------------------------
-- Decisions (Part 12)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
    decision_id                 BIGSERIAL PRIMARY KEY,
    tenant_id                   TEXT NOT NULL REFERENCES tenants(tenant_id),
    event_id                    TEXT REFERENCES raw_events(event_id),
    scope                       TEXT,
    domain                      TEXT,
    decision_type               TEXT,
    classification_confidence   NUMERIC(4,3),
    salience_score              NUMERIC(4,3),
    working_memory_used         BOOLEAN NOT NULL DEFAULT FALSE,
    retrieval_lanes_used        JSONB NOT NULL DEFAULT '[]'::jsonb,
    heuristics_referenced       JSONB NOT NULL DEFAULT '[]'::jsonb,
    similar_decisions_referenced JSONB NOT NULL DEFAULT '[]'::jsonb,
    foundation_files_used       JSONB NOT NULL DEFAULT '[]'::jsonb,
    system_1_answer             TEXT,
    system_2_answer             TEXT,
    divergence_score            NUMERIC(4,3),
    proposed_action             TEXT,
    audit_verdict               TEXT,
    audit_reasoning             TEXT,
    final_action                TEXT,
    autonomy_level_at_time      TEXT,
    owner_action                TEXT,
    historical                  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_decisions_tenant_created ON decisions(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_scope ON decisions(tenant_id, scope);
CREATE INDEX IF NOT EXISTS idx_decisions_divergence ON decisions(divergence_score DESC) WHERE divergence_score IS NOT NULL;

-- --------------------------------------------------------------------------
-- Heuristics (Part 24)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS heuristics (
    heuristic_id        BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    scope               TEXT NOT NULL,
    domain              TEXT,
    condition           TEXT NOT NULL,
    action              TEXT NOT NULL,
    reasoning           TEXT,
    confidence          NUMERIC(4,3) NOT NULL DEFAULT 0.5,
    last_used_at        TIMESTAMPTZ,
    last_retrieved_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source              TEXT NOT NULL,
    provenance          JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              TEXT NOT NULL DEFAULT 'active',
    version             INTEGER NOT NULL DEFAULT 1,
    superseded_by       BIGINT REFERENCES heuristics(heuristic_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_heuristics_tenant_scope ON heuristics(tenant_id, scope, status);
CREATE INDEX IF NOT EXISTS idx_heuristics_status ON heuristics(status);

-- Pending heuristics (proposed by surprise replay or ingestion miner, not yet approved)
CREATE TABLE IF NOT EXISTS pending_heuristics (
    pending_id          BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    scope               TEXT NOT NULL,
    proposed_condition  TEXT NOT NULL,
    proposed_action     TEXT NOT NULL,
    source              TEXT NOT NULL,
    support_count       INTEGER NOT NULL DEFAULT 1,
    evidence_list       JSONB NOT NULL DEFAULT '[]'::jsonb,
    status              TEXT NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- --------------------------------------------------------------------------
-- Skills (Part 17)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS skills (
    skill_id            BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    scope               TEXT NOT NULL,
    domain              TEXT,
    name                TEXT NOT NULL,
    trigger_condition   TEXT,
    steps               JSONB NOT NULL,
    success_criteria    TEXT,
    source_decisions    JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence          NUMERIC(4,3) NOT NULL DEFAULT 0.5,
    version             INTEGER NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- --------------------------------------------------------------------------
-- Predictions (Part 13)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id       BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    decision_id         BIGINT NOT NULL REFERENCES decisions(decision_id),
    prediction_text     TEXT NOT NULL,
    expected_by         TIMESTAMPTZ NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    actual_outcome      TEXT,
    checked_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_predictions_pending ON predictions(status, expected_by) WHERE status = 'pending';

-- --------------------------------------------------------------------------
-- Counterfactuals (Part 13)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS counterfactuals (
    counterfactual_id           BIGSERIAL PRIMARY KEY,
    tenant_id                   TEXT NOT NULL REFERENCES tenants(tenant_id),
    decision_id                 BIGINT NOT NULL REFERENCES decisions(decision_id),
    alternative_choice          TEXT NOT NULL,
    predicted_outcome           TEXT,
    evaluated_at                TIMESTAMPTZ,
    would_have_been_better      BOOLEAN,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- --------------------------------------------------------------------------
-- Mentoring sessions (Part 14)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mentoring_sessions (
    session_id          BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    scheduled_at        TIMESTAMPTZ NOT NULL,
    completed_at        TIMESTAMPTZ,
    questions           JSONB NOT NULL DEFAULT '[]'::jsonb,
    answers             JSONB NOT NULL DEFAULT '[]'::jsonb,
    heuristics_created  JSONB NOT NULL DEFAULT '[]'::jsonb,
    heuristics_updated  JSONB NOT NULL DEFAULT '[]'::jsonb,
    foundation_updates  JSONB NOT NULL DEFAULT '[]'::jsonb,
    mode                TEXT NOT NULL DEFAULT 'ongoing',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- --------------------------------------------------------------------------
-- Audit log (Part 10)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id            BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    decision_id         BIGINT REFERENCES decisions(decision_id),
    verdict             TEXT NOT NULL,
    reasoning           TEXT,
    model_used          TEXT,
    audited_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- --------------------------------------------------------------------------
-- Pending approvals (Part 11)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_approvals (
    approval_id         BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    decision_id         BIGINT NOT NULL REFERENCES decisions(decision_id),
    proposed_action     TEXT NOT NULL,
    expires_at          TIMESTAMPTZ NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pending_approvals_status ON pending_approvals(tenant_id, status, expires_at);

-- --------------------------------------------------------------------------
-- Autonomy state (Part 11)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS autonomy_state (
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    scope               TEXT NOT NULL,
    level               TEXT NOT NULL DEFAULT 'watch',
    since               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_promoted_at    TIMESTAMPTZ,
    last_demoted_at     TIMESTAMPTZ,
    override_rate_7d    NUMERIC(4,3),
    override_rate_30d   NUMERIC(4,3),
    PRIMARY KEY (tenant_id, scope)
);

-- --------------------------------------------------------------------------
-- Regret signals (Part 16, Job 1)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS regret_signals (
    regret_id           BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    decision_id         BIGINT NOT NULL REFERENCES decisions(decision_id),
    heuristic_id        BIGINT REFERENCES heuristics(heuristic_id),
    failure_layer       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- --------------------------------------------------------------------------
-- Fragility log (Part 16, Job 4)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fragility_log (
    fragility_id        BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    heuristic_id        BIGINT NOT NULL REFERENCES heuristics(heuristic_id),
    mutation            TEXT NOT NULL,
    new_action          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- --------------------------------------------------------------------------
-- Private sessions (Part 4 — kill switch audit trail)
-- We log start/end + turn count only, NEVER content.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS private_sessions (
    private_id          BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    session_id          TEXT,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at            TIMESTAMPTZ,
    turn_count          INTEGER NOT NULL DEFAULT 0
);

-- --------------------------------------------------------------------------
-- Cycle log (Part 16 — nightly sleep cycle outcomes)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cycle_log (
    cycle_id            BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    started_at          TIMESTAMPTZ NOT NULL,
    ended_at            TIMESTAMPTZ,
    total_tokens        BIGINT NOT NULL DEFAULT 0,
    per_job             JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- --------------------------------------------------------------------------
-- Embeddings (Part 17)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS embeddings (
    embedding_id        BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    source_table        TEXT NOT NULL,
    source_id           BIGINT NOT NULL,
    vector              vector(384),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_embeddings_lookup ON embeddings(tenant_id, source_table, source_id);
-- Build an HNSW index for fast vector similarity. Cosine distance is the
-- default we'll use in retrieval; switch to l2 if a tenant overrides.
CREATE INDEX IF NOT EXISTS idx_embeddings_vector_hnsw ON embeddings USING hnsw (vector vector_cosine_ops);

-- --------------------------------------------------------------------------
-- Working memory (Part 7) — Postgres TTL fallback when Redis isn't available
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS working_memory (
    wm_key              TEXT NOT NULL,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    payload             JSONB NOT NULL,
    salience            NUMERIC(4,3) NOT NULL DEFAULT 0.5,
    last_touched_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, wm_key)
);
CREATE INDEX IF NOT EXISTS idx_wm_expires ON working_memory(expires_at);

-- --------------------------------------------------------------------------
-- Open items (Part 7) — kept open longer than the WM TTL
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS open_items (
    item_id             BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    scope               TEXT NOT NULL,
    description         TEXT,
    related_decision_id BIGINT REFERENCES decisions(decision_id),
    opened_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at           TIMESTAMPTZ
);

-- --------------------------------------------------------------------------
-- Ingestion jobs and documents (Part 26)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    job_id              BIGSERIAL PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    status              TEXT NOT NULL DEFAULT 'queued',
    document_count      INTEGER NOT NULL DEFAULT 0,
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ingestion_documents (
    document_id         BIGSERIAL PRIMARY KEY,
    job_id              BIGINT NOT NULL REFERENCES ingestion_jobs(job_id),
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    storage_path        TEXT NOT NULL,
    document_type       TEXT,
    period_start        TIMESTAMPTZ,
    period_end          TIMESTAMPTZ,
    participants        JSONB NOT NULL DEFAULT '[]'::jsonb,
    domain              TEXT,
    salience_estimate   NUMERIC(4,3),
    status              TEXT NOT NULL DEFAULT 'queued',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- --------------------------------------------------------------------------
-- Schema version marker
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_meta (
    key                 TEXT PRIMARY KEY,
    value               TEXT NOT NULL
);
INSERT INTO schema_meta(key, value) VALUES ('version', '1')
ON CONFLICT (key) DO NOTHING;

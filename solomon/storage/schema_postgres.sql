-- Postgres overlay. Run AFTER schema.sql when on Postgres.
-- Most of the schema works as-is on Postgres because TEXT, REAL, INTEGER,
-- and JSON-as-TEXT all behave. This overlay upgrades the JSON columns to
-- JSONB and swaps the embeddings.vector column to a real pgvector type.
--
-- The schema.sql is loaded first; this file is loaded second on Postgres only.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Upgrade embeddings.vector from BLOB to pgvector(384).
-- ALTER TABLE is destructive of existing data, so only do it if the column
-- is still BLOB. The CASE handles both fresh installs and re-runs.
DO $$
DECLARE
    coltype TEXT;
BEGIN
    SELECT data_type INTO coltype
    FROM information_schema.columns
    WHERE table_name='embeddings' AND column_name='vector';
    IF coltype = 'bytea' OR coltype = 'BLOB' THEN
        ALTER TABLE embeddings DROP COLUMN vector;
        ALTER TABLE embeddings ADD COLUMN vector vector(384);
    END IF;
END$$;

-- HNSW index for cosine similarity. The retrieval module queries with
-- vector <=> query_vector ORDER BY ... LIMIT k.
CREATE INDEX IF NOT EXISTS idx_embeddings_vector_hnsw
    ON embeddings USING hnsw (vector vector_cosine_ops);

-- Optional: upgrade key JSON-as-TEXT columns to JSONB for query speed.
-- Idempotent: ALTER COLUMN ... TYPE is a no-op when types already match.
ALTER TABLE events
    ALTER COLUMN participants TYPE JSONB USING participants::jsonb,
    ALTER COLUMN channel_metadata TYPE JSONB USING channel_metadata::jsonb,
    ALTER COLUMN classification TYPE JSONB USING classification::jsonb,
    ALTER COLUMN retrieval_context TYPE JSONB USING retrieval_context::jsonb,
    ALTER COLUMN stage_timings_ms TYPE JSONB USING stage_timings_ms::jsonb;

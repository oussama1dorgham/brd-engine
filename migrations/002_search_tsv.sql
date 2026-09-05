-- Fix: the keyword-search index was never used, because the query matched over
-- (req_id || text) while the index covered only (text). Add a STORED generated
-- tsvector column over req_id + text and index THAT, so keyword search hits the
-- GIN index instead of sequential-scanning. Uses the 'simple' config so it also
-- tokenises non-English (e.g. Arabic) content and exact tokens like FR-14.
--
-- Apply to a running DB:  psql "$DATABASE_URL" -f migrations/002_search_tsv.sql

ALTER TABLE brd_chunk
    ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(req_id, '') || ' ' || text)
    ) STORED;

CREATE INDEX IF NOT EXISTS brd_chunk_search_tsv_idx
    ON brd_chunk USING gin (search_tsv);

-- the old index matched the wrong expression and was dead weight
DROP INDEX IF EXISTS brd_chunk_fts_idx;

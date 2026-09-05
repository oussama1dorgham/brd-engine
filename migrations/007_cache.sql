-- Postgres-backed cache for the RAG pipeline's paid / rate-limited remote calls.
-- One advisory key/value table serves three namespaces (see backend/cache.py):
--   embed     (text, model)                -> query vector          [immutable]
--   retrieve  (query, owner, project, ...) -> ranked chunk results  [bust on ingest]
--   answer    (standalone, chunk_ids, gen) -> full response object  [bust on ingest]
--
-- The cache is ALWAYS advisory: a missing/stale/erroring row must degrade to a
-- normal API call, never to a wrong answer. Correctness of the corpus-dependent
-- namespaces comes from busting by (owner_id, project) on every ingest/delete.
--
-- Apply to a running DB:  psql "$DATABASE_URL" -f migrations/007_cache.sql

CREATE TABLE IF NOT EXISTS cache_kv (
    key         text PRIMARY KEY,          -- sha256(namespace + canonical inputs)
    namespace   text NOT NULL,             -- 'embed' | 'retrieve' | 'answer'
    owner_id    int,                       -- NULL for global 'embed' entries
    project     text,                      -- NULL for global 'embed' entries
    value       jsonb NOT NULL,            -- vector | chunk results | response object
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Targeted busting on re-ingest / delete: DELETE ... WHERE namespace + owner_id + project.
CREATE INDEX IF NOT EXISTS cache_kv_bust_idx
    ON cache_kv (namespace, owner_id, project);

-- Optional TTL pruning sweeps by age.
CREATE INDEX IF NOT EXISTS cache_kv_created_idx
    ON cache_kv (created_at);

-- 037 — Round-2 ingestion hardening: upload-rate log survives BRD delete; embedding-cache
-- rows track last USE (not just creation) so grace periods and the new vector-GC sweep don't
-- purge a vector an in-flight job just reused, or one a hit just replayed.
--
-- * ingest_upload_log: append-only, one row per ACCEPTED submit()/reprocess() — unlike
--   ingest_job (deleted wholesale by purge_project_rows on BRD delete), this survives, so the
--   per-hour upload limit can't be reset by deleting and re-uploading. Pruned opportunistically
--   (rows older than 1 day) by the code that inserts into it.
-- * brd_embedding_cache.last_used_at: bumped on every cache hit (embed skip) and every write
--   (new embed, or an edited/added/split requirement's vector — backend/requirements/edit.py).
--   Backfilled from created_at so existing rows aren't immediately GC-eligible.
--
-- brd_real only (FK to app_user). Apply:  python -m backend.migrate

CREATE TABLE IF NOT EXISTS ingest_upload_log (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id   int NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ingest_upload_log_owner_idx ON ingest_upload_log (owner_id, created_at);

ALTER TABLE brd_embedding_cache ADD COLUMN IF NOT EXISTS last_used_at timestamptz;
UPDATE brd_embedding_cache SET last_used_at = created_at WHERE last_used_at IS NULL;
ALTER TABLE brd_embedding_cache ALTER COLUMN last_used_at SET DEFAULT now();
ALTER TABLE brd_embedding_cache ALTER COLUMN last_used_at SET NOT NULL;

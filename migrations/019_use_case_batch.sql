-- Per-batch generation ledger for use cases: makes generation RESUMABLE and
-- corruption-safe. Use-case generation runs scope-by-scope in deterministic
-- BATCHES (one LLM call each; see backend/generate/use_cases.py). When a batch's
-- use cases are written, a 'done' row is committed IN THE SAME TRANSACTION, so a
-- crash / quota stop mid-run leaves only whole, recorded batches. On the next run
-- (mode='resume', the default) batches already marked 'done' are skipped, so the
-- user continues from where it stopped instead of regenerating from the start, and
-- no duplicate use cases are produced. Batches that error or truncate get a
-- 'failed' row (retried on the next resume).
--
-- batch_hash = _reqs_hash(items): a pure function of the batch's requirement
-- chunk_ids + text, so the same batch maps to the same row across runs, and a
-- requirements change (new hash) naturally invalidates the old ledger.
--
-- Owner/project scoped like use_case. brd_real only (FK to app_user).
--
-- Apply:  psql "$DATABASE_URL" -f migrations/019_use_case_batch.sql

CREATE TABLE IF NOT EXISTS use_case_batch (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id   int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project    text NOT NULL,
    scope      text NOT NULL,
    batch_hash text NOT NULL,                 -- _reqs_hash(items): deterministic per batch
    status     text NOT NULL,                 -- 'done' | 'failed'
    uc_count   int  NOT NULL DEFAULT 0,        -- use cases written by this batch
    error      text,                           -- failure detail when status='failed'
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_id, project, batch_hash)
);
CREATE INDEX IF NOT EXISTS use_case_batch_proj_idx ON use_case_batch(owner_id, project);

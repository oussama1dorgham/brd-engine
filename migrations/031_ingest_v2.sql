-- INGESTION v2: structure-aware chunks, stored sources, durable jobs, embedding reuse,
-- staged revisions (compare, then apply or discard), and NON-DESTRUCTIVE requirement deletes.
-- See docs/INGESTION.md. brd_real only (FK to app_user). Re-runnable.
-- Apply:  psql "$DATABASE_URL" -f migrations/031_ingest_v2.sql

-- --- structure on the live chunks -------------------------------------------------
ALTER TABLE brd_chunk ADD COLUMN IF NOT EXISTS section_path text;   -- "Scope / Portfolios / Rules"
ALTER TABLE brd_chunk ADD COLUMN IF NOT EXISTS page int;            -- PDF page the chunk starts on
ALTER TABLE brd_chunk ADD COLUMN IF NOT EXISTS embed_hash text;     -- brd_embedding_cache key of its vector

-- --- DELETES: a removed requirement is ARCHIVED, never hard-deleted --------------
-- The row moves here WITH ITS ORIGINAL id, so every reference (use_case.source_chunk_ids,
-- message.cited_chunk_ids, requirement_change.chunk_id, use-case proposals, cached titles)
-- still resolves; live search / requirement lists no longer see it; and a restore moves it
-- back under the same id. Rows leave this table only when the whole BRD is deleted.
CREATE TABLE IF NOT EXISTS brd_chunk_archive (
    id            bigint PRIMARY KEY,                -- the ORIGINAL brd_chunk.id
    document_id   bigint NOT NULL REFERENCES brd_document(id) ON DELETE CASCADE,
    req_id        text,
    section       text,
    section_path  text,
    page          int,
    ordinal       int,                               -- position it had when it was retired
    text          text NOT NULL,
    token_count   int,
    content_hash  text NOT NULL,
    model         text,
    embedding     vector(1024),                      -- kept so a restore needs no re-embedding
    embed_hash    text,
    retired_at    timestamptz NOT NULL DEFAULT now(),
    retired_by    bigint REFERENCES app_user(id) ON DELETE SET NULL,
    reason        text NOT NULL,                     -- revision | edit | restore | discard
    revision_id   bigint
);
CREATE INDEX IF NOT EXISTS brd_chunk_archive_doc_idx ON brd_chunk_archive (document_id, retired_at DESC);

-- the requirement audit must keep pointing at an archived requirement (was ON DELETE SET NULL)
ALTER TABLE requirement_change DROP CONSTRAINT IF EXISTS requirement_change_chunk_id_fkey;

-- --- stored source files (object storage; see backend/storage.py) ----------------
CREATE TABLE IF NOT EXISTS brd_source (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id     int    NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project      text   NOT NULL,
    document_id  bigint REFERENCES brd_document(id) ON DELETE CASCADE,
    object_key   text   NOT NULL,                    -- brd-sources/<owner>/<sha256><ext>
    filename     text   NOT NULL,
    mime         text,
    size         bigint NOT NULL,
    sha256       text   NOT NULL,
    uploaded_by  bigint REFERENCES app_user(id) ON DELETE SET NULL,
    uploaded_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS brd_source_owner_idx ON brd_source (owner_id, project, uploaded_at DESC);
CREATE INDEX IF NOT EXISTS brd_source_sha_idx   ON brd_source (owner_id, sha256);
ALTER TABLE brd_document ADD COLUMN IF NOT EXISTS source_id bigint REFERENCES brd_source(id) ON DELETE SET NULL;
ALTER TABLE brd_document ADD COLUMN IF NOT EXISTS revision_no int NOT NULL DEFAULT 1;
ALTER TABLE brd_document ADD COLUMN IF NOT EXISTS last_error text;

-- --- durable ingest jobs (claimed with FOR UPDATE SKIP LOCKED; lease-reclaimed) ----
CREATE TABLE IF NOT EXISTS ingest_job (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id         int    NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project          text   NOT NULL,
    document_id      bigint REFERENCES brd_document(id) ON DELETE CASCADE,
    source_id        bigint REFERENCES brd_source(id) ON DELETE CASCADE,
    mode             text   NOT NULL,                -- new | revision
    status           text   NOT NULL DEFAULT 'queued',  -- queued | running | review | done | failed | canceled
    stage            text,                           -- extract | chunk | embed | store | compare
    progress_done    int    NOT NULL DEFAULT 0,
    progress_total   int,
    attempts         int    NOT NULL DEFAULT 0,
    max_attempts     int    NOT NULL DEFAULT 4,
    next_attempt_at  timestamptz NOT NULL DEFAULT now(),
    locked_at        timestamptz,
    error            text,
    warnings         jsonb,
    stats            jsonb,
    created_by       bigint REFERENCES app_user(id) ON DELETE SET NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now()
);
-- one active job per BRD (a second upload while one runs is refused, not raced)
CREATE UNIQUE INDEX IF NOT EXISTS ingest_job_active_uk ON ingest_job (owner_id, project)
    WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS ingest_job_claim_idx ON ingest_job (next_attempt_at)
    WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS ingest_job_owner_idx ON ingest_job (owner_id, project, created_at DESC);

-- --- content-addressed embedding cache: resume after a crash + reuse on re-upload --
-- Keyed per owner so deleting a BRD can purge its vectors (no cross-tenant retention).
CREATE TABLE IF NOT EXISTS brd_embedding_cache (
    owner_id    int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    model       text NOT NULL,
    input_hash  text NOT NULL,                      -- sha256 of the exact text embedded
    embedding   vector(1024) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, model, input_hash)
);

-- --- staged revisions: a re-upload is compared, then applied or discarded -------
CREATE TABLE IF NOT EXISTS brd_revision (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id       bigint NOT NULL REFERENCES brd_document(id) ON DELETE CASCADE,
    owner_id          int    NOT NULL,
    project           text   NOT NULL,
    job_id            bigint REFERENCES ingest_job(id) ON DELETE SET NULL,
    source_id         bigint REFERENCES brd_source(id) ON DELETE SET NULL,
    base_revision_no  int,
    status            text   NOT NULL DEFAULT 'review',   -- review | applied | discarded | superseded
    summary           jsonb,                               -- {same, modified, added, removed}
    created_at        timestamptz NOT NULL DEFAULT now(),
    resolved_at       timestamptz,
    resolved_by       bigint REFERENCES app_user(id) ON DELETE SET NULL,
    resolution        jsonb                                -- what was applied / kept
);
CREATE UNIQUE INDEX IF NOT EXISTS brd_revision_open_uk ON brd_revision (document_id) WHERE status = 'review';
CREATE INDEX IF NOT EXISTS brd_revision_doc_idx ON brd_revision (document_id, created_at DESC);

-- one row per requirement of the NEW version (same | modified | added), plus one per live
-- requirement the new version no longer contains (removed)
CREATE TABLE IF NOT EXISTS brd_revision_item (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    revision_id   bigint NOT NULL REFERENCES brd_revision(id) ON DELETE CASCADE,
    change        text   NOT NULL,                   -- same | modified | added | removed
    ordinal       int,                               -- position in the new version (NULL = removed)
    live_chunk_id bigint,                            -- aligned live requirement (NULL = added)
    live_hash     text,                              -- its content_hash when compared (staleness guard)
    req_id        text,
    section       text,
    section_path  text,
    page          int,
    text          text,                              -- new text (NULL = removed)
    old_text      text,                              -- live text (modified / removed)
    token_count   int,
    content_hash  text,
    embed_hash    text,                              -- brd_embedding_cache key of the new text
    similarity    real
);
CREATE INDEX IF NOT EXISTS brd_revision_item_rev_idx ON brd_revision_item (revision_id, ordinal);

-- (re-runnable) columns added after the first apply
ALTER TABLE brd_chunk_archive ADD COLUMN IF NOT EXISTS embed_hash text;

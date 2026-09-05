-- BRD Retrieval Engine — Phase 0 schema
-- Applied automatically on first container boot (mounted into /docker-entrypoint-initdb.d),
-- or run manually:  psql "$DATABASE_URL" -f migrations/001_init.sql

CREATE EXTENSION IF NOT EXISTS vector;

-- 1) Source documents: one row per BRD version -------------------------------
CREATE TABLE IF NOT EXISTS brd_document (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title       TEXT        NOT NULL,
    project     TEXT        NOT NULL,
    version     TEXT        NOT NULL DEFAULT 'v1',
    status      TEXT        NOT NULL DEFAULT 'draft',   -- draft | approved | superseded
    source_uri  TEXT,
    sha256      TEXT        NOT NULL,                   -- dedupe unchanged documents
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project, title, version)
);

-- 2) Retrievable units, structure preserved ----------------------------------
CREATE TABLE IF NOT EXISTS brd_chunk (
    id           BIGINT     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id  BIGINT     NOT NULL REFERENCES brd_document(id) ON DELETE CASCADE,
    req_id       TEXT,                                  -- e.g. FR-14, NFR-3
    section      TEXT,                                  -- e.g. "Functional Requirements"
    ordinal      INT        NOT NULL,                   -- order within the document
    text         TEXT       NOT NULL,
    token_count  INT,
    content_hash TEXT       NOT NULL,                   -- skip re-embedding unchanged chunks
    UNIQUE (document_id, ordinal)
);
CREATE INDEX IF NOT EXISTS brd_chunk_document_idx ON brd_chunk (document_id);
CREATE INDEX IF NOT EXISTS brd_chunk_hash_idx     ON brd_chunk (content_hash);
-- keyword (BM25-style) half of hybrid search
CREATE INDEX IF NOT EXISTS brd_chunk_fts_idx
    ON brd_chunk USING gin (to_tsvector('english', text));

-- 3) The searchable vector, one per chunk -------------------------------------
CREATE TABLE IF NOT EXISTS brd_embedding (
    chunk_id   BIGINT       PRIMARY KEY REFERENCES brd_chunk(id) ON DELETE CASCADE,
    model      TEXT         NOT NULL,
    embedding  vector(1024) NOT NULL                    -- voyage-4 default dimension
);
-- approximate nearest-neighbour index (cosine distance)
CREATE INDEX IF NOT EXISTS brd_embedding_hnsw_idx
    ON brd_embedding USING hnsw (embedding vector_cosine_ops);

-- 4) Chat sessions + turns, with provenance -----------------------------------
CREATE TABLE IF NOT EXISTS conversation (
    id            BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       TEXT,
    project_scope TEXT,                                 -- restrict retrieval to a project
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS message (
    id              BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id BIGINT      NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
    role            TEXT        NOT NULL,               -- user | assistant
    text            TEXT        NOT NULL,
    cited_chunk_ids BIGINT[],                           -- which chunks each answer cited
    usage_json      JSONB,                              -- token usage / cost per turn
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS message_conversation_idx ON message (conversation_id);

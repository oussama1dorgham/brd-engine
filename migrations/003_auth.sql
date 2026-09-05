-- Auth: users + server-side sessions, and per-user BRD ownership.
-- Apply to an existing DB:  psql "$DATABASE_URL" -f migrations/003_auth.sql

CREATE TABLE IF NOT EXISTS app_user (
    id            BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email         TEXT        UNIQUE NOT NULL,
    password_hash TEXT        NOT NULL,               -- argon2id
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_session (
    id         TEXT        PRIMARY KEY,               -- secrets.token_urlsafe(32)
    user_id    BIGINT      NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS user_session_user_idx ON user_session(user_id);

-- Per-user BRD ownership. Existing rows get owner_id = NULL (invisible to all
-- users — effectively retired; re-upload under an account). New uploads set it.
ALTER TABLE brd_document ADD COLUMN IF NOT EXISTS owner_id BIGINT REFERENCES app_user(id) ON DELETE CASCADE;

-- Uniqueness is now per-owner (two users may each have a "es" project).
ALTER TABLE brd_document DROP CONSTRAINT IF EXISTS brd_document_project_title_version_key;
CREATE UNIQUE INDEX IF NOT EXISTS brd_document_owner_ptv_idx ON brd_document(owner_id, project, title, version);
CREATE INDEX IF NOT EXISTS brd_document_owner_idx ON brd_document(owner_id);

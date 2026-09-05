-- Email verification + password reset: a verified flag and one-time tokens.
-- Apply:  psql "$DATABASE_URL" -f migrations/004_auth_tokens.sql

ALTER TABLE app_user ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS auth_token (
    token      TEXT        PRIMARY KEY,           -- secrets.token_urlsafe(32)
    user_id    BIGINT      NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    kind       TEXT        NOT NULL CHECK (kind IN ('verify', 'reset')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS auth_token_user_idx ON auth_token(user_id);

-- Service accounts + scoped API tokens for external (machine) access.
--
-- A service account is a NON-HUMAN identity linked to a human (owner_id). It is
-- granted a subset of that owner's BRDs (service_account_project) and a set of
-- scopes per token. Requests act AS the owner (so existing per-owner isolation is
-- reused) but are additionally fenced by scope + granted project + rate limit.
--
-- Tokens authenticate with a bearer header. Only a SHA-256 hash is stored (the raw
-- token is shown once at issue); `masked` is a display-only hint. Human sessions /
-- OTP are unaffected. brd_real only (FK to app_user).
--
-- Apply:  psql "$DATABASE_URL" -f migrations/017_service_accounts.sql

CREATE TABLE IF NOT EXISTS service_account (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id    int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    name        text NOT NULL,
    disabled    boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS service_account_owner_idx ON service_account(owner_id);

-- Which of the owner's BRDs (projects) this account may access.
CREATE TABLE IF NOT EXISTS service_account_project (
    service_account_id bigint NOT NULL REFERENCES service_account(id) ON DELETE CASCADE,
    project            text   NOT NULL,
    PRIMARY KEY (service_account_id, project)
);

CREATE TABLE IF NOT EXISTS api_token (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    service_account_id bigint NOT NULL REFERENCES service_account(id) ON DELETE CASCADE,
    token_hash         text NOT NULL UNIQUE,               -- sha256 hex of the raw token
    masked             text NOT NULL,                      -- display hint, e.g. "brdsk_…ab12"
    scopes             jsonb NOT NULL DEFAULT '[]'::jsonb, -- e.g. ["ask","read"]
    rate_limit_per_min int  NOT NULL DEFAULT 60,
    expires_at         timestamptz,                        -- NULL = no expiry
    revoked_at         timestamptz,                        -- NULL = active
    last_used_at       timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS api_token_sa_idx ON api_token(service_account_id);

-- One row per token-authenticated request (attribution / audit).
CREATE TABLE IF NOT EXISTS api_audit (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    token_id   bigint REFERENCES api_token(id) ON DELETE SET NULL,
    endpoint   text NOT NULL,
    project    text,
    status     int  NOT NULL,
    ip         text,
    at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS api_audit_token_idx ON api_audit(token_id, at);

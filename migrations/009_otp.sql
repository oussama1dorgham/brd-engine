-- Emailed 6-digit OTP codes for signup verification, password reset, and periodic
-- login step-up (see backend/otp.py). Codes are stored HASHED (argon2), expire
-- quickly, and are attempt-capped to resist brute force of the small code space.
--
-- login_count drives the "require a code every Nth login" rule (OTP_LOGIN_EVERY).
--
-- Apply to a running DB:  psql "$DATABASE_URL" -f migrations/009_otp.sql

ALTER TABLE app_user ADD COLUMN IF NOT EXISTS login_count int NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS otp_code (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       int NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    kind          text NOT NULL,              -- 'signup' | 'login' | 'reset'
    code_hash     text NOT NULL,              -- argon2 hash of the 6-digit code
    expires_at    timestamptz NOT NULL,
    attempts      int NOT NULL DEFAULT 0,
    max_attempts  int NOT NULL DEFAULT 5,
    consumed_at   timestamptz,               -- set on success, exhaustion, or supersede
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- "latest outstanding code for this user+kind" lookups.
CREATE INDEX IF NOT EXISTS otp_code_lookup_idx
    ON otp_code (user_id, kind, consumed_at);

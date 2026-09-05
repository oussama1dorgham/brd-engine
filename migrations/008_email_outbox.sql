-- Durable email outbox with FIXED-interval retry (see backend/email_outbox.py).
-- Auth email (verification / reset) is written here first, then sent; a failed send
-- is retried by a background sweeper instead of being lost. Scoped PER USER /
-- RECIPIENT (authentication is per-user; sessions don't exist yet at signup/reset).
--
-- Race-freedom: a row is claimed with an atomic UPDATE ... status='sending' guarded
-- on the current status, so the immediate attempt and the sweeper (and multiple
-- workers) can never double-send. A lease (locked_at) lets a crashed worker's row
-- be reclaimed. Delivery is at-least-once.
--
-- Apply to a running DB:  psql "$DATABASE_URL" -f migrations/008_email_outbox.sql

CREATE TABLE IF NOT EXISTS email_outbox (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         int REFERENCES app_user(id) ON DELETE SET NULL,  -- per-user; null for session-less/forgot
    recipient_email text NOT NULL,
    subject         text NOT NULL,
    body            text NOT NULL,        -- plain-text part
    html            text,                 -- optional HTML alternative
    status          text NOT NULL DEFAULT 'pending',  -- pending | sending | sent | failed
    attempts        int  NOT NULL DEFAULT 0,          -- incremented at CLAIM time
    max_attempts    int  NOT NULL,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    locked_at       timestamptz,          -- lease start while status='sending'
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    sent_at         timestamptz,
    failed_at       timestamptz           -- dead-letter time; drives per-recipient cooldown
);

-- Claim query: due 'pending' rows and stale-leased 'sending' rows, by schedule.
CREATE INDEX IF NOT EXISTS email_outbox_due_idx
    ON email_outbox (status, next_attempt_at);

-- Per-recipient cooldown lookups after a dead-letter.
CREATE INDEX IF NOT EXISTS email_outbox_recipient_idx
    ON email_outbox (recipient_email, status);

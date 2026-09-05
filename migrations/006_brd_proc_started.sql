-- When the current processing run began (for the "~N min left" ETA).
-- Apply:  psql "$DATABASE_URL" -f migrations/006_brd_proc_started.sql

ALTER TABLE brd_document ADD COLUMN IF NOT EXISTS proc_started_at TIMESTAMPTZ;

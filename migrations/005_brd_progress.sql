-- Ingestion progress: embedded-chunk counter for the "Processing NN%" UI.
-- Apply:  psql "$DATABASE_URL" -f migrations/005_brd_progress.sql

ALTER TABLE brd_document ADD COLUMN IF NOT EXISTS chunks_total INT;
ALTER TABLE brd_document ADD COLUMN IF NOT EXISTS chunks_done  INT NOT NULL DEFAULT 0;

-- Classify each use case as a functional (happy-path), negative (invalid input /
-- unauthorized / error-handling) or edge (boundary / concurrency / unusual-but-valid)
-- test case, so a generated suite covers each requirement from all three angles.
-- Existing rows default to 'functional' (what the old generator produced). brd_real only.
--
-- Apply:  psql "$DATABASE_URL" -f migrations/026_use_case_type.sql

ALTER TABLE use_case
    ADD COLUMN IF NOT EXISTS case_type text NOT NULL DEFAULT 'functional';

CREATE INDEX IF NOT EXISTS use_case_type_idx ON use_case(owner_id, project, case_type);

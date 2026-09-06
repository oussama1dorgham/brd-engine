-- Multiple BYOK keys per user: switch user_llm_key from one-row-per-user to
-- many-rows-per-user with a surrogate id, an is_active flag (one active per user),
-- and an optional label. preferred_models stays per-row (each key its own subset).
-- Existing single keys become the active key for their user.
--
-- Apply to a running DB:  psql "$DATABASE_URL" -f migrations/013_llm_multi_key.sql

DO $$
BEGIN
  IF NOT EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name = 'user_llm_key' AND column_name = 'id') THEN
    ALTER TABLE user_llm_key DROP CONSTRAINT IF EXISTS user_llm_key_pkey;      -- old PK on user_id
    ALTER TABLE user_llm_key ADD COLUMN id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY;
  END IF;
END $$;

ALTER TABLE user_llm_key ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true;
ALTER TABLE user_llm_key ADD COLUMN IF NOT EXISTS label text;

CREATE INDEX IF NOT EXISTS user_llm_key_user_idx ON user_llm_key (user_id);
-- at most one active key per user
CREATE UNIQUE INDEX IF NOT EXISTS user_llm_key_one_active ON user_llm_key (user_id) WHERE is_active;

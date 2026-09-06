-- Preferred models for the bring-your-own-key (BYOK) LLM feature.
-- After a user validates their key we list every model it can reach; this column
-- stores the SUBSET they curated in the "choose your models" modal. The in-chat
-- model picker shows this subset (Claude-Desktop style); an empty list means
-- "not curated yet" and the picker falls back to showing all available models.
--
-- Stored as a JSON array of model-id strings, order-preserved.
-- brd_real only (FK chain to app_user via user_llm_key).
--
-- Apply to a running DB:  psql "$DATABASE_URL" -f migrations/011_llm_preferred_models.sql

ALTER TABLE user_llm_key
    ADD COLUMN IF NOT EXISTS preferred_models jsonb NOT NULL DEFAULT '[]'::jsonb;

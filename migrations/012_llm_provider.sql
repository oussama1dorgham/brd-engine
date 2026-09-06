-- Multi-provider BYOK: record which provider a stored key belongs to, so the
-- right adapter is used (openai-compatible | anthropic | gemini | cohere).
-- Existing rows default to 'openai' (the OpenAI-compatible adapter), matching
-- how keys were interpreted before this column existed.
--
-- Apply to a running DB:  psql "$DATABASE_URL" -f migrations/012_llm_provider.sql

ALTER TABLE user_llm_key ADD COLUMN IF NOT EXISTS provider text NOT NULL DEFAULT 'openai';

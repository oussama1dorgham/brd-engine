-- Custom LLM (bring-your-own-key), OpenAI-compatible providers only.
-- Each user may store ONE provider key: an OpenAI-style base_url + an API key that
-- is ENCRYPTED at rest (Fernet, key from LLM_KEY_SECRET — see backend/crypto.py).
-- api_key_masked is a display-only hint (e.g. "sk-…abcd"); the plaintext key is
-- never stored and never returned to the client.
--
-- conversation.model records the model picked for that chat; NULL falls back to
-- the user's provider default / the system GEN_MODEL.
--
-- Apply to a running DB:  psql "$DATABASE_URL" -f migrations/010_llm_keys.sql

CREATE TABLE IF NOT EXISTS user_llm_key (
    user_id        int PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
    base_url       text NOT NULL,
    api_key_enc    text NOT NULL,      -- Fernet ciphertext of the provider API key
    api_key_masked text NOT NULL,      -- display-only, e.g. "sk-…abcd"
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE conversation ADD COLUMN IF NOT EXISTS model text;

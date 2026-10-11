-- 036 — Stable embed-context title, independent of the display title.
--
-- The embed input is "<title> › <section path>\n\n<text>" (backend/ingest/chunker.py
-- embed_input_text). Using the LIVE title there means renaming a BRD (backend/chat/
-- history_store.py rename, which only touches `title`) changed every requirement's embed
-- input and forced a full re-embed. `context_title` is set once at creation (submit(), see
-- backend/ingest/jobs.py) and never touched by rename; ingestion and requirement edits read
-- coalesce(context_title, title) instead of `title` directly, so pre-existing BRDs (backfilled
-- below, context_title = title at the time of this migration) keep their current hashes too.
--
-- brd_real only (FK to app_user). Apply:  python -m backend.migrate

ALTER TABLE brd_document ADD COLUMN IF NOT EXISTS context_title text;
UPDATE brd_document SET context_title = title WHERE context_title IS NULL;

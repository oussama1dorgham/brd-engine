-- Change-a-requirement Phase 2: structural edits (split an over-long edit into
-- several chunks, add a requirement, remove one). Inserting between existing
-- chunks means shifting ordinals, which transiently duplicates (document_id, ordinal)
-- under a non-deferrable constraint. Make that uniqueness DEFERRABLE so the shift +
-- inserts are validated once, at commit.
--
-- Also record what kind of change each audit row represents.
--
-- Apply to a running DB:  psql "$DATABASE_URL" -f migrations/015_requirement_structural.sql

ALTER TABLE brd_chunk DROP CONSTRAINT IF EXISTS brd_chunk_document_id_ordinal_key;  -- 001's inline name
ALTER TABLE brd_chunk DROP CONSTRAINT IF EXISTS brd_chunk_doc_ordinal_uk;
ALTER TABLE brd_chunk ADD CONSTRAINT brd_chunk_doc_ordinal_uk
    UNIQUE (document_id, ordinal) DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE requirement_change ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'edit';  -- edit | add | delete

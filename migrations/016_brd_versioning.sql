-- Change-a-requirement Phase 3: document versioning + approval.
-- review_status: 'approved' (live/clean) or 'draft' (has un-reviewed edits).
-- brd_snapshot: an immutable, restorable copy of a document's full chunk set —
-- text AND embedding, so a restore is exact and needs no re-embedding. Used both
-- for manual versions and for the approve/discard (reject) safety net.
--
-- Apply to a running DB:  psql "$DATABASE_URL" -f migrations/016_brd_versioning.sql

ALTER TABLE brd_document ADD COLUMN IF NOT EXISTS review_status TEXT NOT NULL DEFAULT 'approved';  -- approved | draft

CREATE TABLE IF NOT EXISTS brd_snapshot (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id BIGINT      NOT NULL REFERENCES brd_document(id) ON DELETE CASCADE,
    label       TEXT,
    kind        TEXT        NOT NULL DEFAULT 'manual',   -- manual | approved | auto (pre-restore baseline)
    created_by  BIGINT      REFERENCES app_user(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    data        JSONB       NOT NULL   -- [{req_id, section, ordinal, text, model, embedding(text literal)}]
);
CREATE INDEX IF NOT EXISTS brd_snapshot_doc_idx ON brd_snapshot (document_id, created_at DESC);

-- Change-a-requirement (QA): audit trail for in-place requirement edits, plus a
-- brd_document.updated_at touched on each change. Each edit re-embeds the chunk and
-- busts the project's caches (see backend/ingest/edit.py); this table is the
-- traceability record (who changed what, from → to, when).
--
-- Apply to a running DB:  psql "$DATABASE_URL" -f migrations/014_requirement_change.sql

ALTER TABLE brd_document ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS requirement_change (
    id           BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id  BIGINT      NOT NULL REFERENCES brd_document(id) ON DELETE CASCADE,
    chunk_id     BIGINT      REFERENCES brd_chunk(id) ON DELETE SET NULL,
    req_id       TEXT,
    old_text     TEXT,
    new_text     TEXT        NOT NULL,
    changed_by   BIGINT      REFERENCES app_user(id) ON DELETE SET NULL,
    changed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS requirement_change_chunk_idx ON requirement_change (chunk_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS requirement_change_doc_idx   ON requirement_change (document_id, changed_at DESC);

-- Change-request aggregate (Phase B): a story-driven requirement change is a single
-- unit of intent, so it gets a PARENT row that (a) records its provenance — the
-- plain-language story, the refined instruction, the generating model, the author —
-- and (b) is the idempotency anchor: an apply is deduped on (owner_id,
-- idempotency_key), so a retried/double-submitted apply replays the stored result
-- instead of re-running (which would duplicate "add" requirements). Each audit row
-- in requirement_change links back to its change_request, grouping "why did these
-- requirements change together?" into one record.
--
-- The row is written INSIDE the same transaction as the chunk writes (see
-- backend/ingest/edit.py apply_change_set), so its existence is proof the change
-- committed: a rolled-back attempt leaves no row, and a retry proceeds fresh.
--
-- brd_real only (FK to app_user). Apply:  psql "$DATABASE_URL" -f migrations/025_change_request.sql

CREATE TABLE IF NOT EXISTS change_request (
    id               BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id         BIGINT      NOT NULL REFERENCES app_user(id)     ON DELETE CASCADE,
    document_id      BIGINT      NOT NULL REFERENCES brd_document(id) ON DELETE CASCADE,
    project          TEXT        NOT NULL,
    story            TEXT,                       -- the user's plain-language request (provenance)
    refined          TEXT,                       -- the single refined instruction it was planned from
    model            TEXT,                       -- the generation model that produced the plan
    idempotency_key  TEXT        NOT NULL,        -- client-supplied or derived from the operations
    status           TEXT        NOT NULL DEFAULT 'applied',
    applied          INT         NOT NULL DEFAULT 0,
    result           JSONB,                      -- the stored apply summary, replayed on a duplicate
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS change_request_owner_key_uk ON change_request (owner_id, idempotency_key);
CREATE INDEX        IF NOT EXISTS change_request_doc_idx      ON change_request (document_id, created_at DESC);

ALTER TABLE requirement_change ADD COLUMN IF NOT EXISTS change_id BIGINT REFERENCES change_request(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS requirement_change_change_idx ON requirement_change (change_id);

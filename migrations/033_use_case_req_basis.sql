-- 033 — Requirement-level basis for incremental use-case generation.
--
-- One row per requirement a BRD's use cases were last generated (or updated) from, with the
-- md5 of its text at that moment. Comparing it with the live requirements says EXACTLY which
-- requirements are new, changed or removed — so an update regenerates only the use cases that
-- depend on those, in place (same uid/code), instead of whole 15-requirement batches with new ids.
--
-- A requirement is written here in the SAME transaction as the use cases generated from it, so
-- an interrupted run simply leaves the rest pending: the next Update continues, never duplicates.
-- Backfilled lazily by the app (from the old batch ledger + existing citations) the first time a
-- BRD is looked at. brd_real only (FK to app_user).
-- Apply:  python -m backend.migrate

CREATE TABLE IF NOT EXISTS use_case_req_basis (
    owner_id    int    NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project     text   NOT NULL,
    chunk_id    bigint NOT NULL,                 -- brd_chunk.id (ids survive edits / revisions / restores)
    hash        text   NOT NULL,                 -- md5(requirement text) when last generated from
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, project, chunk_id)
);

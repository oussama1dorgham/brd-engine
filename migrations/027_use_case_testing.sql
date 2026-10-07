-- Use-case TEST EXECUTION: status lifecycle + evidence (proofs) + activity timeline.
-- See docs/USECASE_TESTING.md. Requires 020 (use_case.uid) and 021 (use_case_version).
--
-- A use case is a TEST case. This adds status dimensions ORTHOGONAL to the existing
-- authoring lifecycle (use_case.status = draft|approved). The model has ONE ordered
-- execution axis plus two derived/secondary signals — deliberately NOT a flat soup:
--
--   * EXECUTION LIFECYCLE (use_case.test_status), mutually exclusive, one order:
--       not_tested -> in_progress -> blocked -> failed -> passed
--     Every value answers the same question ("what happened when we ran it").
--   * NEEDS-RETEST is DERIVED, not a status: a verdict whose tested_against_ver is older
--     than the card's current definition version is stale. Computed on read (no column),
--     shown as a badge over the last verdict — so "passed but stale" keeps its verdict.
--   * SCOPE (use_case.in_scope) is a SEPARATE axis: an out-of-scope card is excluded from
--     coverage math (it is not a verdict). Toggled with an auditable 'scope' timeline event.
--
-- All of this is kept OUT of the definition version snapshot (_FIELDS in
-- backend/use_case_versioning.py) and is never rolled back by a definition restore.
--
-- Design (freedom-first, see docs §12):
--   * use_case.test_status   — denormalized CURRENT status, for fast tree render + filter.
--     Source of truth + history is use_case_test_result; this is a cache of the latest.
--   * use_case_test_result   — one immutable row per timeline EVENT, keyed by the stable
--     use_case_uid so it survives a delete -> restore just like versions do. event_kind
--     is 'status' (a verdict change) or 'note' (a comment/log entry, no verdict) so the
--     timeline is a full ACTIVITY LOG and evidence can be logged before any verdict.
--     tested_against_ver pins the definition version the result was recorded against, so a
--     later edit/requirement-change can flag the result as stale (retest).
--   * use_case_test_proof    — evidence bound to a specific result EVENT (so proofs stay
--     with "the failed"), N per event, mixed kinds. caption + ordinal for labelled,
--     reorderable proofs. A file kind stores `content` (bytea); a 'link' stores `url`.
--
-- brd_real only (FK to app_user). No backfill needed — absence of a result = 'not_tested'.
--
-- Apply:  psql "$DATABASE_URL" -f migrations/027_use_case_testing.sql

-- --- denormalized current status + scope axis --------------------------------
ALTER TABLE use_case ADD COLUMN IF NOT EXISTS test_status text NOT NULL DEFAULT 'not_tested';
ALTER TABLE use_case ADD COLUMN IF NOT EXISTS in_scope boolean NOT NULL DEFAULT true;
CREATE INDEX IF NOT EXISTS use_case_test_status_idx ON use_case(owner_id, project, test_status);

-- --- execution timeline (status changes + notes) -----------------------------
CREATE TABLE IF NOT EXISTS use_case_test_result (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    use_case_uid       bigint NOT NULL,                          -- use_case.uid (stable across delete->restore)
    owner_id           int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project            text NOT NULL,
    event_kind         text NOT NULL DEFAULT 'status',           -- status | note | scope
    status             text NOT NULL,                            -- not_tested|in_progress|blocked|failed|passed (carried forward on note/scope events)
    note               text,
    in_scope           boolean,                                  -- only set on a 'scope' event: the value scope was toggled TO
    tested_against_ver int,                                      -- use_case_version.version_no at record time
    tested_by          bigint REFERENCES app_user(id) ON DELETE SET NULL,
    tested_at          timestamptz NOT NULL DEFAULT now()
);
-- (re-runnable) add the scope columns to a use_case_test_result created before this revision
ALTER TABLE use_case_test_result ADD COLUMN IF NOT EXISTS in_scope boolean;
CREATE INDEX IF NOT EXISTS use_case_test_result_uid_idx  ON use_case_test_result(use_case_uid, tested_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS use_case_test_result_feed_idx ON use_case_test_result(owner_id, project, tested_at DESC);

-- --- evidence bound to a result event ----------------------------------------
CREATE TABLE IF NOT EXISTS use_case_test_proof (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    result_id     bigint NOT NULL REFERENCES use_case_test_result(id) ON DELETE CASCADE,
    use_case_uid  bigint NOT NULL,                               -- denormalized: "all proofs for this card"
    owner_id      int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    kind          text NOT NULL,                                 -- image|video|file|link
    filename      text,
    mime          text,
    size          int,
    content       bytea,                                         -- file bytes (NULL for kind='link')
    url           text,                                          -- external link (NULL for file kinds)
    caption       text,
    ordinal       int  NOT NULL DEFAULT 0,
    uploaded_by   bigint REFERENCES app_user(id) ON DELETE SET NULL,
    uploaded_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS use_case_test_proof_result_idx ON use_case_test_proof(result_id, ordinal, id);
CREATE INDEX IF NOT EXISTS use_case_test_proof_uid_idx    ON use_case_test_proof(use_case_uid);

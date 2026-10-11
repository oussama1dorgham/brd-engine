-- Use-case TEST EXECUTION, round 2: test cycles (traceability), content-hash retest clock,
-- evidence staging, defects, amend/undo. See docs/USECASE_TESTING.md §13. Requires 027.
--
--   * RETEST CLOCK = CONTENT HASH, not version_no. `use_case.def_hash` is a GENERATED column
--     over ONLY the fields that change what a tester does (preconditions, steps, expected
--     behaviour, roles, source requirements). A move / title rename / draft->approved /
--     delete->restore / no-op save no longer raises "needs retest"; restoring a card back to
--     the exact definition it was tested against correctly clears it. Every write path
--     (edit, restore, regenerate) keeps it current for free — no code has to remember to.
--     A verdict pins `tested_against_hash`; `use_case.tested_hash` caches the current
--     verdict's pin, so needs_retest is a plain column compare (no joins on tree load).
--   * EVIDENCE STAGING: use_case_test_proof.result_id is now NULLABLE. A proof with no
--     result is PENDING — staged in the composer — and is claimed by the next verdict/note
--     event. Evidence captured before the verdict therefore lands on the verdict it proves.
--   * TEST CYCLES: a named run (e.g. "Sprint 12 regression") with a planned membership. A
--     verdict recorded while a cycle is active carries cycle_id, so a cycle has its own
--     per-case status + report while use_case.test_status stays the latest verdict overall.
--     Deleting a cycle never deletes evidence (cycle_id -> NULL = ad-hoc run).
--   * DEFECTS: a failed/blocked verdict can carry a first-class defect reference.
--   * AMEND/UNDO: `amended_from` remembers the verdict an in-window re-verdict replaced.
--
-- brd_real only (FK to app_user). Re-runnable.
-- Apply:  psql "$DATABASE_URL" -f migrations/028_test_cycles.sql

-- --- content-hash retest clock -------------------------------------------------
ALTER TABLE use_case ADD COLUMN IF NOT EXISTS def_hash text GENERATED ALWAYS AS (
    md5(coalesce(preconditions, '') || chr(31) || coalesce(steps::text, '') || chr(31) ||
        coalesce(expected_behaviour, '') || chr(31) || coalesce(roles::text, '') || chr(31) ||
        coalesce(source_chunk_ids::text, ''))
) STORED;
ALTER TABLE use_case ADD COLUMN IF NOT EXISTS tested_hash text;          -- def_hash the current verdict was recorded against
ALTER TABLE use_case_test_result ADD COLUMN IF NOT EXISTS tested_against_hash text;

-- Backfill: results recorded before this migration are treated as tested against the
-- CURRENT definition (the version-number clock they used was unreliable — see B1).
UPDATE use_case_test_result r SET tested_against_hash = uc.def_hash
  FROM use_case uc WHERE uc.uid = r.use_case_uid AND r.tested_against_hash IS NULL;
UPDATE use_case SET tested_hash = def_hash
  WHERE tested_hash IS NULL AND test_status IN ('blocked', 'failed', 'passed');

-- --- evidence staging + project-scoped cleanup --------------------------------
ALTER TABLE use_case_test_proof ALTER COLUMN result_id DROP NOT NULL;
ALTER TABLE use_case_test_proof ADD COLUMN IF NOT EXISTS project text;
UPDATE use_case_test_proof p SET project = r.project
  FROM use_case_test_result r WHERE r.id = p.result_id AND p.project IS NULL;
CREATE INDEX IF NOT EXISTS use_case_test_proof_pending_idx
  ON use_case_test_proof(use_case_uid, ordinal, id) WHERE result_id IS NULL;
CREATE INDEX IF NOT EXISTS use_case_test_proof_proj_idx ON use_case_test_proof(owner_id, project);

-- --- test cycles ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS test_cycle (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id     int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project      text NOT NULL,
    name         text NOT NULL,
    description  text,
    status       text NOT NULL DEFAULT 'open',                     -- open | closed
    created_by   bigint REFERENCES app_user(id) ON DELETE SET NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    closed_at    timestamptz
);
CREATE INDEX IF NOT EXISTS test_cycle_proj_idx ON test_cycle(owner_id, project, created_at DESC);

-- planned membership: which cases this run covers, and the definition each was planned at
CREATE TABLE IF NOT EXISTS test_cycle_case (
    cycle_id      bigint NOT NULL REFERENCES test_cycle(id) ON DELETE CASCADE,
    use_case_uid  bigint NOT NULL,
    planned_hash  text,                                            -- def_hash when planned in
    added_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (cycle_id, use_case_uid)
);

ALTER TABLE use_case_test_result ADD COLUMN IF NOT EXISTS cycle_id bigint REFERENCES test_cycle(id) ON DELETE SET NULL;
ALTER TABLE use_case_test_result ADD COLUMN IF NOT EXISTS defect_ref text;
ALTER TABLE use_case_test_result ADD COLUMN IF NOT EXISTS amended_from text;   -- verdict an in-window re-verdict replaced (undo)
ALTER TABLE use_case_test_result ADD COLUMN IF NOT EXISTS amended_at timestamptz;
CREATE INDEX IF NOT EXISTS use_case_test_result_cycle_idx
  ON use_case_test_result(cycle_id, use_case_uid, tested_at DESC, id DESC) WHERE cycle_id IS NOT NULL;

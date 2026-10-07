-- Per-use-case AUDIT LOG: one append-only record of EVERYTHING that happened to a use case —
-- who did it, when, what, and the status before/after. See docs/USECASE_TESTING.md §14.
--
-- Why a separate table: the timeline tables are working state, not an audit — an in-window
-- re-verdict AMENDS its row, an undo DELETES it, a proof removal deletes the proof. The audit
-- keeps the trace of every one of those acts, so the history is complete and attributable:
--   category  definition | test | evidence | scope | cycle | proposal
--   action    create|edit|move|delete|restore|regenerate (definition, = use_case_version.change_kind)
--             status_recorded|status_amended|status_reverted|note_added|entry_undone|entry_edited (test)
--             proof_added|proof_removed|proof_relabelled (evidence) · scope_out|scope_in (scope)
--             cycle_planned|cycle_unplanned (cycle) · proposal_opened|proposal_accepted|proposal_dismissed
--   source    user | regeneration (the generation job acting for the user who started it)
-- The actor's email is denormalized so an entry stays readable even if the account is removed.
-- APPEND-ONLY: a trigger rejects UPDATE; rows are deleted only when the whole BRD is purged.
--
-- Backfills from use_case_version, use_case_test_result and use_case_test_proof (idempotent).
-- brd_real only (FK to app_user). Re-runnable.
-- Apply:  psql "$DATABASE_URL" -f migrations/030_use_case_audit.sql

CREATE TABLE IF NOT EXISTS use_case_audit (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    use_case_uid   bigint NOT NULL,
    owner_id       int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project        text NOT NULL,
    uc_id          text,
    actor_id       bigint REFERENCES app_user(id) ON DELETE SET NULL,
    actor_email    text,
    source         text NOT NULL DEFAULT 'user',
    category       text NOT NULL,
    action         text NOT NULL,
    summary        text,
    status_before  text,
    status_after   text,
    cycle_id       bigint,                  -- no FK: the audit outlives a deleted cycle
    cycle_name     text,
    version_no     int,                     -- definition entries: the use_case_version it produced
    result_id      bigint,                  -- test entries: the timeline event (may since be amended/undone)
    detail         jsonb,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS use_case_audit_uid_idx  ON use_case_audit(use_case_uid, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS use_case_audit_proj_idx ON use_case_audit(owner_id, project, created_at DESC);

CREATE OR REPLACE FUNCTION use_case_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'use_case_audit is append-only';
END $$;
DROP TRIGGER IF EXISTS use_case_audit_no_update ON use_case_audit;
CREATE TRIGGER use_case_audit_no_update BEFORE UPDATE ON use_case_audit
    FOR EACH ROW EXECUTE FUNCTION use_case_audit_append_only();

-- --- backfill: definition versions ------------------------------------------------
INSERT INTO use_case_audit (use_case_uid, owner_id, project, uc_id, actor_id, actor_email, source,
                            category, action, summary, version_no, created_at)
SELECT v.use_case_uid, v.owner_id, v.project, v.uc_id, v.changed_by, u.email,
       CASE WHEN v.change_kind IN ('create', 'regenerate') THEN 'regeneration' ELSE 'user' END,
       'definition', v.change_kind, v.change_summary, v.version_no, v.changed_at
FROM use_case_version v LEFT JOIN app_user u ON u.id = v.changed_by
WHERE NOT EXISTS (SELECT 1 FROM use_case_audit a WHERE a.category = 'definition'
                  AND a.use_case_uid = v.use_case_uid AND a.version_no = v.version_no);

-- --- backfill: test timeline events -------------------------------------------------
WITH lbl(k, v) AS (VALUES ('not_tested', 'Not run'), ('in_progress', 'In progress'), ('blocked', 'Blocked'),
                          ('failed', 'Failed'), ('passed', 'Passed')),
ev AS (
    SELECT r.*, CASE WHEN r.event_kind = 'status' THEN
             lag(r.status) OVER (PARTITION BY r.use_case_uid, (r.event_kind = 'status') ORDER BY r.tested_at, r.id)
           END AS prev_status
    FROM use_case_test_result r
)
INSERT INTO use_case_audit (use_case_uid, owner_id, project, uc_id, actor_id, actor_email, source, category,
                            action, summary, status_before, status_after, cycle_id, cycle_name, result_id,
                            detail, created_at)
SELECT e.use_case_uid, e.owner_id, e.project, (SELECT uc_id FROM use_case WHERE uid = e.use_case_uid LIMIT 1),
       e.tested_by, u.email, 'user',
       CASE e.event_kind WHEN 'scope' THEN 'scope' ELSE 'test' END,
       CASE e.event_kind WHEN 'status' THEN 'status_recorded' WHEN 'note' THEN 'note_added'
            ELSE CASE WHEN e.in_scope THEN 'scope_in' ELSE 'scope_out' END END,
       CASE e.event_kind WHEN 'status' THEN 'Marked ' || coalesce((SELECT v FROM lbl WHERE k = e.status), e.status)
            WHEN 'note' THEN 'Added a note'
            ELSE CASE WHEN e.in_scope THEN 'Returned to scope' ELSE 'Marked out of scope' END END,
       coalesce(e.prev_status, CASE WHEN e.event_kind = 'status' THEN 'not_tested' END),
       CASE WHEN e.event_kind = 'status' THEN e.status END,
       e.cycle_id, c.name, e.id,
       nullif(jsonb_strip_nulls(jsonb_build_object('note', e.note, 'defect_ref', e.defect_ref)), '{}'::jsonb),
       e.tested_at
FROM ev e LEFT JOIN app_user u ON u.id = e.tested_by LEFT JOIN test_cycle c ON c.id = e.cycle_id
WHERE NOT EXISTS (SELECT 1 FROM use_case_audit a WHERE a.category IN ('test', 'scope') AND a.result_id = e.id);

-- --- backfill: evidence ------------------------------------------------------------------
INSERT INTO use_case_audit (use_case_uid, owner_id, project, uc_id, actor_id, actor_email, source, category,
                            action, summary, result_id, detail, created_at)
SELECT p.use_case_uid, p.owner_id, coalesce(p.project, ''), (SELECT uc_id FROM use_case WHERE uid = p.use_case_uid LIMIT 1),
       p.uploaded_by, u.email, 'user', 'evidence', 'proof_added',
       'Attached ' || coalesce(p.filename, p.url, p.kind), p.result_id,
       jsonb_build_object('proof_id', p.id, 'kind', p.kind), p.uploaded_at
FROM use_case_test_proof p LEFT JOIN app_user u ON u.id = p.uploaded_by
WHERE NOT EXISTS (SELECT 1 FROM use_case_audit a WHERE a.action = 'proof_added'
                  AND (a.detail->>'proof_id')::bigint = p.id);

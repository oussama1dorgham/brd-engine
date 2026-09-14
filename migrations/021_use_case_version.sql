-- Use-case VERSION HISTORY + audit log — Phase 0 of use-case change management.
-- See docs/USECASE_VERSIONING.md. Requires 020 (use_case.uid, use_case.batch_hash).
--
-- ONE full-snapshot table serves three needs at once:
--   * version history / backup  — each row is a complete, immutable copy of a card's
--     state at a point in time (title, steps, citations, folder path, …), so any prior
--     state is exactly restorable.
--   * audit log ("what changed") — derived by DIFFING consecutive versions of the same
--     use_case_uid on read; `change_summary` precomputes a one-line label for the list.
--   * regeneration safety net    — replace/sync snapshot each card (kind='regenerate')
--     BEFORE deleting it, so no regeneration is ever destructive.
--
-- The core invariant (enforced in backend/use_case_versioning.py): no use case is
-- deleted or overwritten without first recording a version IN THE SAME TRANSACTION.
--
-- History OUTLIVES the card: owner_id/project/uc_id are denormalized and use_case_id
-- is ON DELETE SET NULL, so a deleted card's versions remain (that is the backup, and
-- what powers the "Trash" / restore-deleted view). Keyed by use_case_uid, not the row
-- PK, so the thread is continuous across a delete -> restore.
--
-- brd_real only (FK to app_user).
--
-- Apply:  psql "$DATABASE_URL" -f migrations/021_use_case_version.sql

CREATE TABLE IF NOT EXISTS use_case_version (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    use_case_uid   bigint NOT NULL,                              -- stable logical id (use_case.uid)
    use_case_id    bigint REFERENCES use_case(id) ON DELETE SET NULL,  -- live row at capture, nulled on delete
    owner_id       int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project        text NOT NULL,
    uc_id          text,                                         -- "UC-12" label at capture
    version_no     int  NOT NULL,                                -- 1,2,3… per use_case_uid
    change_kind    text NOT NULL,                                -- create|edit|move|delete|regenerate|restore
    change_summary text,                                         -- e.g. "title, steps changed"
    changed_by     bigint REFERENCES app_user(id) ON DELETE SET NULL,
    changed_at     timestamptz NOT NULL DEFAULT now(),
    data           jsonb NOT NULL,   -- {title, description, roles, preconditions, steps,
                                     --  expected_behaviour, source_chunk_ids, folder_path,
                                     --  status, batch_hash}
    UNIQUE (use_case_uid, version_no)
);
CREATE INDEX IF NOT EXISTS use_case_version_uid_idx  ON use_case_version(use_case_uid, version_no DESC);
CREATE INDEX IF NOT EXISTS use_case_version_feed_idx ON use_case_version(owner_id, project, changed_at DESC);

-- Optional whole-tree snapshot: one row captures the entire tree before a full
-- 'replace' regenerate, so the previous tree can be restored in one click (mirrors
-- brd_snapshot). Per-card history above is the primary backup; this is the coarse undo.
CREATE TABLE IF NOT EXISTS use_case_tree_snapshot (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id   int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project    text NOT NULL,
    label      text,
    kind       text NOT NULL DEFAULT 'pre-regenerate',   -- pre-regenerate | manual | auto
    created_by bigint REFERENCES app_user(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    data       jsonb NOT NULL   -- {folders:[{uid?,name,parent_path,ordinal}], use_cases:[{...full card...}]}
);
CREATE INDEX IF NOT EXISTS use_case_tree_snapshot_idx ON use_case_tree_snapshot(owner_id, project, created_at DESC);

-- --- backfill: give every existing card a version 1 so history exists from day one ---
INSERT INTO use_case_version
    (use_case_uid, use_case_id, owner_id, project, uc_id, version_no,
     change_kind, change_summary, changed_at, data)
SELECT uc.uid, uc.id, uc.owner_id, uc.project, uc.uc_id, 1,
       'create', 'Initial (backfilled)', uc.created_at,
       jsonb_build_object(
         'title', uc.title,
         'description', uc.description,
         'roles', uc.roles,
         'preconditions', uc.preconditions,
         'steps', uc.steps,
         'expected_behaviour', uc.expected_behaviour,
         'source_chunk_ids', uc.source_chunk_ids,
         'status', uc.status,
         'batch_hash', uc.batch_hash,
         'folder_path', concat_ws(' / ', pf.name, f.name)
       )
FROM use_case uc
JOIN use_case_folder f  ON f.id  = uc.folder_id
LEFT JOIN use_case_folder pf ON pf.id = f.parent_id
WHERE NOT EXISTS (SELECT 1 FROM use_case_version v WHERE v.use_case_uid = uc.uid);

-- One-time (idempotent) cleanup of data orphaned by BRD deletions that predate the
-- "delete a BRD → delete its conversations + use cases" fix.
--
-- An orphan is anything scoped to a (project, owner) for which no brd_document exists. This
-- removes: conversations (messages cascade) and every use-case artifact (cases, folders,
-- ledger, version history, tree snapshots). Safe to re-run — it only ever deletes true
-- orphans, so subsequent runs delete nothing. Use-case tables are guarded (brd_real only).
--
-- Apply:  psql "$DATABASE_URL" -f migrations/024_cleanup_orphans.sql

-- conversations orphaned by a deleted BRD (numeric user_id == app_user.id)
DELETE FROM conversation c
WHERE c.project_scope IS NOT NULL
  AND c.user_id ~ '^[0-9]+$'
  AND NOT EXISTS (
    SELECT 1 FROM brd_document d
    WHERE d.project = c.project_scope AND d.owner_id = c.user_id::int
  );

-- use-case artifacts orphaned by a deleted BRD
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'use_case_version', 'use_case_tree_snapshot', 'use_case_batch', 'use_case', 'use_case_folder'
  ] LOOP
    IF to_regclass('public.' || t) IS NOT NULL THEN
      EXECUTE format(
        'DELETE FROM %I x WHERE NOT EXISTS ('
        '  SELECT 1 FROM brd_document d WHERE d.project = x.project AND d.owner_id = x.owner_id)',
        t);
    END IF;
  END LOOP;
END $$;

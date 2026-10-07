-- 034 — Test basis + version-bound runs.
--
-- "Needs retest" used to compare a verdict's pin with use_case.def_hash — the test-relevant
-- fields only — so a REQUIREMENT edited under an unchanged case never asked for a retest.
-- use_case.test_basis = md5(def_hash + the md5 of every cited requirement's text), computed
-- exactly like a version content row's basis (use_case_content_basis), and kept current by
-- triggers: when a case's test fields change, and when a cited requirement's text changes,
-- is removed/archived, or comes back. Every pin (use_case.tested_hash, a result's
-- tested_against_hash, a cycle member's planned_hash) now holds a basis.
--
-- Runs: a result records the version content it tested (content_id, 032) and, when it was
-- carried into a new cycle unchanged, the result it was carried from (carried_from).
--
-- brd_real only (FK to app_user). Apply:  python -m backend.migrate

CREATE OR REPLACE FUNCTION use_case_test_basis(p_pre text, p_steps jsonb, p_exp text, p_roles jsonb, p_cids jsonb)
RETURNS text LANGUAGE sql STABLE AS $$
    -- the inner md5 is use_case.def_hash's generated expression (028), byte for byte
    SELECT use_case_content_basis(
        md5(coalesce(p_pre, '') || chr(31) || coalesce(p_steps::text, '') || chr(31) ||
            coalesce(p_exp, '') || chr(31) || coalesce(p_roles::text, '') || chr(31) ||
            coalesce(p_cids::text, '')),
        use_case_req_hashes(p_cids))
$$;

ALTER TABLE use_case ADD COLUMN IF NOT EXISTS test_basis text;
CREATE INDEX IF NOT EXISTS use_case_cites_gin ON use_case USING gin (source_chunk_ids jsonb_path_ops);

CREATE OR REPLACE FUNCTION use_case_set_test_basis() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.test_basis := use_case_test_basis(NEW.preconditions, NEW.steps, NEW.expected_behaviour,
                                          NEW.roles, NEW.source_chunk_ids);
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS use_case_test_basis_trg ON use_case;
CREATE TRIGGER use_case_test_basis_trg
    BEFORE INSERT OR UPDATE OF preconditions, steps, expected_behaviour, roles, source_chunk_ids ON use_case
    FOR EACH ROW EXECUTE FUNCTION use_case_set_test_basis();

-- a requirement's text changed / it was removed, archived or restored → refresh the cases citing it
CREATE OR REPLACE FUNCTION use_case_requirement_changed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_id := OLD.id;
    ELSE
        IF TG_OP = 'UPDATE' AND NEW.text IS NOT DISTINCT FROM OLD.text THEN
            RETURN NULL;
        END IF;
        v_id := NEW.id;
    END IF;
    UPDATE use_case SET test_basis = use_case_test_basis(preconditions, steps, expected_behaviour, roles, source_chunk_ids)
     WHERE source_chunk_ids @> jsonb_build_array(v_id) OR source_chunk_ids @> jsonb_build_array(v_id::text);
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS use_case_req_changed_trg ON brd_chunk;
CREATE TRIGGER use_case_req_changed_trg AFTER INSERT OR DELETE OR UPDATE OF text ON brd_chunk
    FOR EACH ROW EXECUTE FUNCTION use_case_requirement_changed();
DROP TRIGGER IF EXISTS use_case_req_archived_trg ON brd_chunk_archive;
CREATE TRIGGER use_case_req_archived_trg AFTER INSERT OR DELETE ON brd_chunk_archive
    FOR EACH ROW EXECUTE FUNCTION use_case_requirement_changed();

ALTER TABLE use_case_test_result ADD COLUMN IF NOT EXISTS carried_from bigint
    REFERENCES use_case_test_result(id) ON DELETE SET NULL;

-- --- backfill ---------------------------------------------------------------------------
UPDATE use_case SET test_basis = use_case_test_basis(preconditions, steps, expected_behaviour, roles, source_chunk_ids)
 WHERE test_basis IS NULL;
-- pins taken against the old def_hash-only definition → the same definition's basis (requirement
-- text as of now: its history before this migration is unknown). Stale pins stay stale.
UPDATE use_case SET tested_hash = test_basis WHERE tested_hash IS NOT NULL AND tested_hash = def_hash;
UPDATE use_case_test_result r SET tested_against_hash = uc.test_basis
  FROM use_case uc WHERE uc.uid = r.use_case_uid AND r.tested_against_hash = uc.def_hash;
UPDATE test_cycle_case m SET planned_hash = uc.test_basis
  FROM use_case uc WHERE uc.uid = m.use_case_uid AND m.planned_hash = uc.def_hash;

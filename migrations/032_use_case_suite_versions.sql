-- 032 — Version-controlled use-case suites.
--
-- The live use_case rows are the single editable DRAFT (working copy). Publishing freezes the
-- draft into an immutable SUITE VERSION: a list of (case uid → content row, folder, position).
-- Runs (test_cycle) and results bind to a version, so "what exactly was tested" is always exact.
--
--   use_case_suite_version  one row per published version (number 1,2,3… per BRD)
--   use_case_content        one row per UNIQUE case content (content-addressed, shared by versions)
--   use_case_req_text       the cited requirement text, stored once per unique text
--   use_case_suite_member   version ⇄ content membership + folder path + ordinal
--
-- Two hashes per content row:
--   content_hash  every definition field (title, description, type, roles, preconditions, steps,
--                 expected behaviour, citations) → drives "modified" in a version diff
--   def_hash      only the test-relevant fields (= use_case.def_hash) → drives whether an earlier
--                 result is still valid (a title typo fix is a change, but not a retest)
-- plus req_hashes = md5 of every cited requirement's text at capture, folded into basis_hash
-- (the dedup key): a case whose own text is unchanged but whose requirement was edited gets a
-- NEW content row, and the version still shows the requirement exactly as it was.
--
-- Immutability: content, members and requirement texts can never be UPDATEd; a version row
-- only lets status/label change. Rows are DELETEd only when the whole BRD is purged.
--
-- brd_real only (FK to app_user).
-- Apply:  python -m backend.migrate

CREATE TABLE IF NOT EXISTS use_case_suite_version (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id         int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project          text NOT NULL,
    number           int  NOT NULL,                        -- v1, v2… per (owner, project)
    parent_id        bigint REFERENCES use_case_suite_version(id) ON DELETE SET NULL,
    status           text NOT NULL DEFAULT 'published' CHECK (status IN ('published', 'archived')),
    source           text NOT NULL,                        -- baseline | publish | restore
    label            text,
    brd_document_id  bigint,                               -- the BRD (and its revision) it was built on
    brd_revision_no  int,
    counts           jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {cases, functional, negative, edge}
    created_by       bigint REFERENCES app_user(id) ON DELETE SET NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_id, project, number)
);
CREATE INDEX IF NOT EXISTS use_case_suite_version_proj_idx ON use_case_suite_version(owner_id, project, number DESC);

CREATE TABLE IF NOT EXISTS use_case_req_text (
    owner_id  int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project   text NOT NULL,
    hash      text NOT NULL,                               -- md5(text)
    req_id    text,
    text      text NOT NULL,
    PRIMARY KEY (owner_id, project, hash)
);

CREATE TABLE IF NOT EXISTS use_case_content (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id      int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project       text NOT NULL,
    use_case_uid  bigint NOT NULL,                         -- the case's stable identity (use_case.uid)
    content_hash  text NOT NULL,
    def_hash      text NOT NULL,
    req_hashes    jsonb NOT NULL DEFAULT '{}'::jsonb,      -- {"<chunk id>": "<md5 of its text>"}
    basis_hash    text NOT NULL,                           -- md5(content_hash + req_hashes): dedup key
    data          jsonb NOT NULL,                          -- {uc_id, title, description, case_type, roles,
                                                           --  preconditions, steps, expected_behaviour, source_chunk_ids}
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (use_case_uid, basis_hash)
);
CREATE INDEX IF NOT EXISTS use_case_content_proj_idx ON use_case_content(owner_id, project);

CREATE TABLE IF NOT EXISTS use_case_suite_member (
    suite_version_id  bigint NOT NULL REFERENCES use_case_suite_version(id) ON DELETE CASCADE,
    use_case_uid      bigint NOT NULL,
    content_id        bigint NOT NULL REFERENCES use_case_content(id),   -- RESTRICT: content never vanishes under a version
    folder_path       jsonb  NOT NULL DEFAULT '[]'::jsonb,               -- folder names root→leaf at capture
    ordinal           int    NOT NULL DEFAULT 0,
    PRIMARY KEY (suite_version_id, use_case_uid)
);
CREATE INDEX IF NOT EXISTS use_case_suite_member_content_idx ON use_case_suite_member(content_id);

-- runs and results bind to what they tested (RESTRICT: a version with runs can't be deleted)
ALTER TABLE test_cycle ADD COLUMN IF NOT EXISTS suite_version_id bigint
    REFERENCES use_case_suite_version(id) ON DELETE RESTRICT;
ALTER TABLE use_case_test_result ADD COLUMN IF NOT EXISTS content_id bigint
    REFERENCES use_case_content(id) ON DELETE RESTRICT;
-- audit entries name the version they happened in (NULL = before versioning existed)
ALTER TABLE use_case_audit ADD COLUMN IF NOT EXISTS suite_version_id bigint;   -- no FK: the audit outlives a purge

-- --- immutability ------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION use_case_suite_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is immutable (publish a new version instead)', TG_TABLE_NAME;
END $$;

DROP TRIGGER IF EXISTS use_case_content_no_update ON use_case_content;
CREATE TRIGGER use_case_content_no_update BEFORE UPDATE ON use_case_content
    FOR EACH ROW EXECUTE FUNCTION use_case_suite_immutable();
DROP TRIGGER IF EXISTS use_case_suite_member_no_update ON use_case_suite_member;
CREATE TRIGGER use_case_suite_member_no_update BEFORE UPDATE ON use_case_suite_member
    FOR EACH ROW EXECUTE FUNCTION use_case_suite_immutable();
DROP TRIGGER IF EXISTS use_case_req_text_no_update ON use_case_req_text;
CREATE TRIGGER use_case_req_text_no_update BEFORE UPDATE ON use_case_req_text
    FOR EACH ROW EXECUTE FUNCTION use_case_suite_immutable();

CREATE OR REPLACE FUNCTION use_case_suite_version_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.owner_id, NEW.project, NEW.number, NEW.parent_id, NEW.source, NEW.brd_document_id,
        NEW.brd_revision_no, NEW.counts, NEW.created_by, NEW.created_at)
       IS DISTINCT FROM
       (OLD.owner_id, OLD.project, OLD.number, OLD.parent_id, OLD.source, OLD.brd_document_id,
        OLD.brd_revision_no, OLD.counts, OLD.created_by, OLD.created_at)
       AND NOT (OLD.parent_id IS NOT NULL AND NEW.parent_id IS NULL) THEN   -- ON DELETE SET NULL of a purged parent
        RAISE EXCEPTION 'a published suite version is immutable (only status and label may change)';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS use_case_suite_version_guard ON use_case_suite_version;
CREATE TRIGGER use_case_suite_version_guard BEFORE UPDATE ON use_case_suite_version
    FOR EACH ROW EXECUTE FUNCTION use_case_suite_version_guard();

-- --- cited requirement hashes: {"<chunk id>": md5(text)} (live text, else archived; null if gone) ---
-- The ONE definition, shared by version content rows and use_case.test_basis (034), so "the
-- requirement changed" means the same thing everywhere.
CREATE OR REPLACE FUNCTION use_case_req_hashes(p_cids jsonb) RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT coalesce(jsonb_object_agg(c.id::text, (
               SELECT md5(t.text) FROM (SELECT b.text, 0 AS pri FROM brd_chunk b WHERE b.id = c.id
                                        UNION ALL
                                        SELECT a.text, 1 FROM brd_chunk_archive a WHERE a.id = c.id) t
                ORDER BY t.pri LIMIT 1)), '{}'::jsonb)
      FROM (SELECT DISTINCT e.v::bigint AS id
              FROM jsonb_array_elements_text(CASE WHEN jsonb_typeof(p_cids) = 'array' THEN p_cids
                                                  ELSE '[]'::jsonb END) e(v)
             WHERE e.v ~ '^\d+$') c
$$;

-- a content row's test basis: its test-relevant definition + the requirement texts it cites.
-- STRICT: NULL in (e.g. an outer join that found no content row) → NULL out, so "this case is not
-- in that version" stays detectable instead of hashing to a bogus basis.
CREATE OR REPLACE FUNCTION use_case_content_basis(p_def_hash text, p_req_hashes jsonb) RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT md5(p_def_hash || chr(31) || p_req_hashes::text) $$;

-- --- the draft, as version rows: the ONE definition of "what would be published" ------------
-- Used by the snapshot below AND by the app's draft-vs-version diff and publish staleness check,
-- so they can never disagree. `reqs` carries the cited requirement texts (live or archived —
-- archived rows keep their id and text) for storing alongside a new version.
CREATE OR REPLACE FUNCTION use_case_draft_rows(p_owner int, p_project text)
RETURNS TABLE (uid bigint, ordinal int, folder_path jsonb, def_hash text, data jsonb,
               content_hash text, req_hashes jsonb, basis_hash text, reqs jsonb)
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE fp AS (
        SELECT f.id, jsonb_build_array(f.name) AS path FROM use_case_folder f
         WHERE f.owner_id = p_owner AND f.project = p_project AND f.parent_id IS NULL
        UNION ALL
        SELECT f.id, fp.path || jsonb_build_array(f.name) FROM use_case_folder f JOIN fp ON f.parent_id = fp.id
    ), cite AS (
        SELECT DISTINCT u.uid AS case_uid, e.v::bigint AS chunk_id
          FROM use_case u CROSS JOIN LATERAL jsonb_array_elements_text(u.source_chunk_ids) e(v)
         WHERE u.owner_id = p_owner AND u.project = p_project AND e.v ~ '^\d+$'
    ), req AS (
        SELECT DISTINCT ON (r.id) r.id AS chunk_id, r.req_id, r.text, md5(r.text) AS hash
          FROM (SELECT c.id, c.req_id, c.text, 0 AS pri FROM brd_chunk c
                 WHERE c.id IN (SELECT chunk_id FROM cite)
                UNION ALL
                SELECT a.id, a.req_id, a.text, 1 FROM brd_chunk_archive a
                 WHERE a.id IN (SELECT chunk_id FROM cite)) r
         ORDER BY r.id, r.pri
    ), base AS (
        SELECT u.uid AS case_uid, u.ordinal AS ord, coalesce(fp.path, '[]'::jsonb) AS fpath,
               coalesce(u.def_hash, '') AS dhash,
               jsonb_build_object('uc_id', u.uc_id, 'title', u.title, 'description', u.description,
                                  'case_type', u.case_type, 'roles', u.roles,
                                  'preconditions', u.preconditions, 'steps', u.steps,
                                  'expected_behaviour', u.expected_behaviour,
                                  'source_chunk_ids', u.source_chunk_ids) AS body,
               md5(concat_ws(chr(31), u.title, u.description, u.case_type, u.roles::text,
                             u.preconditions, u.steps::text, u.expected_behaviour,
                             u.source_chunk_ids::text)) AS chash,
               use_case_req_hashes(u.source_chunk_ids) AS rhashes,
               coalesce((SELECT jsonb_agg(jsonb_build_object('hash', q.hash, 'req_id', q.req_id, 'text', q.text))
                           FROM cite c JOIN req q ON q.chunk_id = c.chunk_id
                          WHERE c.case_uid = u.uid), '[]'::jsonb) AS rtexts
          FROM use_case u LEFT JOIN fp ON fp.id = u.folder_id
         WHERE u.owner_id = p_owner AND u.project = p_project
    )
    SELECT case_uid, ord, fpath, dhash, body, chash, rhashes,
           md5(chash || chr(31) || rhashes::text), rtexts
      FROM base
$$;

-- --- snapshot: freeze the live use cases of one BRD into a new version ---------------------
-- The ONE place a version is built (baseline below, publish from the app). Serialized per BRD
-- with an advisory lock. If nothing differs from the latest version it creates nothing and
-- returns that version's id (publishing twice is a no-op).
CREATE OR REPLACE FUNCTION use_case_snapshot(p_owner int, p_project text, p_by bigint,
                                             p_source text, p_label text)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    v_parent bigint; v_num int; v_doc bigint; v_rev int; v_id bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('use_case_suite:' || p_owner || ':' || p_project));

    SELECT id, number INTO v_parent, v_num FROM use_case_suite_version
     WHERE owner_id = p_owner AND project = p_project ORDER BY number DESC LIMIT 1;
    SELECT id, revision_no INTO v_doc, v_rev FROM brd_document
     WHERE owner_id = p_owner AND project = p_project ORDER BY id DESC LIMIT 1;

    CREATE TEMP TABLE _cur ON COMMIT DROP AS SELECT * FROM use_case_draft_rows(p_owner, p_project);

    INSERT INTO use_case_req_text (owner_id, project, hash, req_id, text)
    SELECT DISTINCT ON (r->>'hash') p_owner, p_project, r->>'hash', r->>'req_id', r->>'text'
      FROM _cur CROSS JOIN LATERAL jsonb_array_elements(_cur.reqs) r
    ON CONFLICT DO NOTHING;

    INSERT INTO use_case_content (owner_id, project, use_case_uid, content_hash, def_hash,
                                  req_hashes, basis_hash, data)
    SELECT p_owner, p_project, uid, content_hash, coalesce(def_hash, ''), req_hashes, basis_hash, data
      FROM _cur
    ON CONFLICT (use_case_uid, basis_hash) DO NOTHING;

    INSERT INTO use_case_suite_version (owner_id, project, number, parent_id, source, label,
                                        brd_document_id, brd_revision_no, counts, created_by)
    SELECT p_owner, p_project, coalesce(v_num, 0) + 1, v_parent, p_source, p_label, v_doc, v_rev,
           jsonb_build_object('cases', count(*),
                              'functional', count(*) FILTER (WHERE data->>'case_type' = 'functional'),
                              'negative',   count(*) FILTER (WHERE data->>'case_type' = 'negative'),
                              'edge',       count(*) FILTER (WHERE data->>'case_type' = 'edge')),
           p_by
      FROM _cur
    RETURNING id INTO v_id;

    INSERT INTO use_case_suite_member (suite_version_id, use_case_uid, content_id, folder_path, ordinal)
    SELECT v_id, c.uid, k.id, c.folder_path, c.ordinal
      FROM _cur c JOIN use_case_content k ON k.use_case_uid = c.uid AND k.basis_hash = c.basis_hash;

    -- nothing changed since the latest version → keep that one, create nothing
    IF v_parent IS NOT NULL AND NOT EXISTS (
        (SELECT use_case_uid, content_id, folder_path, ordinal FROM use_case_suite_member WHERE suite_version_id = v_id
         EXCEPT
         SELECT use_case_uid, content_id, folder_path, ordinal FROM use_case_suite_member WHERE suite_version_id = v_parent)
        UNION ALL
        (SELECT use_case_uid, content_id, folder_path, ordinal FROM use_case_suite_member WHERE suite_version_id = v_parent
         EXCEPT
         SELECT use_case_uid, content_id, folder_path, ordinal FROM use_case_suite_member WHERE suite_version_id = v_id)
    ) THEN
        DELETE FROM use_case_suite_version WHERE id = v_id;
        v_id := v_parent;
    END IF;

    DROP TABLE IF EXISTS _cur;
    RETURN v_id;
END $$;

-- --- baseline: today's use cases become v1 of every BRD that has any ----------------------
SELECT use_case_snapshot(p.owner_id, p.project, NULL, 'baseline', 'Baseline — use cases before versioning')
  FROM (SELECT DISTINCT owner_id, project FROM use_case) p
 WHERE NOT EXISTS (SELECT 1 FROM use_case_suite_version v
                    WHERE v.owner_id = p.owner_id AND v.project = p.project);

-- existing test cycles ran against the pre-versioning cards: bind them to that BRD's v1
UPDATE test_cycle c SET suite_version_id = v.id
  FROM use_case_suite_version v
 WHERE c.suite_version_id IS NULL AND v.owner_id = c.owner_id AND v.project = c.project AND v.number = 1;

-- existing results → the v1 content they tested, when they tested exactly that definition
-- (an older definition than v1 stays NULL: it was tested before versioning and isn't in any version)
UPDATE use_case_test_result r SET content_id = k.id
  FROM use_case_suite_version v
  JOIN use_case_suite_member m ON m.suite_version_id = v.id
  JOIN use_case_content k ON k.id = m.content_id
 WHERE r.content_id IS NULL AND v.owner_id = r.owner_id AND v.project = r.project AND v.number = 1
   AND m.use_case_uid = r.use_case_uid
   AND r.tested_against_hash IN (k.def_hash, use_case_content_basis(k.def_hash, k.req_hashes));   -- pre-/post-034 pins

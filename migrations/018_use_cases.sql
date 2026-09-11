-- Use cases generated from a BRD, organized as a folder tree (folders/subfolders)
-- with the use cases as leaves. Each use case is traceable back to the requirement
-- chunks it derives from (source_chunk_ids). Owner/project scoped like requirements.
-- Users may edit everything after generation. brd_real only (FK to app_user).
--
-- Apply:  psql "$DATABASE_URL" -f migrations/018_use_cases.sql

CREATE TABLE IF NOT EXISTS use_case_folder (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id   int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project    text NOT NULL,
    parent_id  bigint REFERENCES use_case_folder(id) ON DELETE CASCADE,  -- NULL = top level (a scope)
    name       text NOT NULL,
    ordinal    int  NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS use_case_folder_proj_idx ON use_case_folder(owner_id, project);
CREATE INDEX IF NOT EXISTS use_case_folder_parent_idx ON use_case_folder(parent_id);

CREATE TABLE IF NOT EXISTS use_case (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id           int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project            text NOT NULL,
    folder_id          bigint REFERENCES use_case_folder(id) ON DELETE CASCADE,
    uc_id              text,                               -- stable label e.g. "UC-12"
    title              text NOT NULL,
    description        text NOT NULL DEFAULT '',
    roles              jsonb NOT NULL DEFAULT '[]'::jsonb,
    preconditions      text NOT NULL DEFAULT '',
    steps              jsonb NOT NULL DEFAULT '[]'::jsonb,
    expected_behaviour text NOT NULL DEFAULT '',
    source_chunk_ids   jsonb NOT NULL DEFAULT '[]'::jsonb, -- traceability -> brd_chunk.id
    ordinal            int  NOT NULL DEFAULT 0,
    status             text NOT NULL DEFAULT 'draft',       -- draft | approved
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS use_case_proj_idx ON use_case(owner_id, project);
CREATE INDEX IF NOT EXISTS use_case_folder_fk_idx ON use_case(folder_id);

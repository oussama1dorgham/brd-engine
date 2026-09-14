-- Use-case STABLE IDENTITY + batch linkage — Phase 0 of use-case change management
-- (versioning + audit + incremental regeneration). See docs/USECASE_VERSIONING.md.
--
-- Two additions to `use_case`:
--
--   uid        A stable LOGICAL identity for a use case, independent of the row PK.
--              It is set once at creation and PRESERVED across edits and across a
--              delete -> restore cycle, so a card's version history is one continuous
--              thread even after the physical row is deleted and later re-inserted.
--              Backed by a plain sequence with a DEFAULT (not GENERATED ALWAYS) so a
--              restore can re-insert a card with its ORIGINAL uid. Unique across live
--              rows: at most one live use_case per uid at any time.
--
--   batch_hash The _reqs_hash of the generation batch that produced this card
--              (see backend/generate/use_cases.py). Links a card back to its batch so
--              incremental regeneration ("sync") can find and supersede exactly the
--              cards whose requirements changed, instead of wiping the whole tree.
--
-- Owner/project scoped like use_case. brd_real only (FK to app_user via use_case).
--
-- Apply:  psql "$DATABASE_URL" -f migrations/020_use_case_identity.sql

-- --- batch linkage -----------------------------------------------------------
ALTER TABLE use_case ADD COLUMN IF NOT EXISTS batch_hash text;
CREATE INDEX IF NOT EXISTS use_case_batch_hash_idx ON use_case(owner_id, project, batch_hash);

-- --- stable logical identity -------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS use_case_uid_seq;
ALTER TABLE use_case ADD COLUMN IF NOT EXISTS uid bigint;

-- backfill any existing rows before enforcing NOT NULL
UPDATE use_case SET uid = nextval('use_case_uid_seq') WHERE uid IS NULL;

ALTER TABLE use_case ALTER COLUMN uid SET DEFAULT nextval('use_case_uid_seq');
ALTER TABLE use_case ALTER COLUMN uid SET NOT NULL;

-- one LIVE row per uid (a deleted card frees its uid for its own restore to reuse)
CREATE UNIQUE INDEX IF NOT EXISTS use_case_uid_uk ON use_case(uid);

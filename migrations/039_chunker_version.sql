-- 039: which ingestion (chunker) version built a BRD's requirements.
--
-- Set when a BRD is first stored and when a reviewed new version is applied
-- (backend.ingest.chunker.CHUNKER_VERSION). A BRD built by an older version — NULL: before
-- this column, i.e. every BRD ingested until now — is flagged in Manage BRDs with an
-- "Improve: re-process / upload original again" action, because its chunks may be split by size
-- instead of by requirement (headless documents especially).
--
-- Idempotent.  Apply:  psql "$DATABASE_URL" -f migrations/039_chunker_version.sql

ALTER TABLE brd_document ADD COLUMN IF NOT EXISTS chunker_version integer;

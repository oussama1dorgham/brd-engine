-- 038: keyword search also matches a requirement's LABEL (section_path).
--
-- Headless BRDs carry a requirement's / block's label ("US-01: Access the Companion Calendar by
-- Role", "Shared Business Rules / Audit") in section_path, not in the chunk text — so a later
-- piece of a long requirement, or a business-rule paragraph, is only findable by its label if
-- the label is indexed. Rebuilds the generated search_tsv over normalized
-- (req_id + section + section_path + text).
--
-- Idempotent: the column is only rebuilt when its expression does not include section_path yet
-- (re-running on each deploy does not rewrite the table).
--
-- Apply:  psql "$DATABASE_URL" -f migrations/038_search_tsv_section_path.sql

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'brd_chunk' AND column_name = 'search_tsv'
      AND generation_expression LIKE '%section_path%'
  ) THEN
    ALTER TABLE brd_chunk DROP COLUMN IF EXISTS search_tsv;
    ALTER TABLE brd_chunk ADD COLUMN search_tsv tsvector
      GENERATED ALWAYS AS (
        to_tsvector('simple',
          brd_normalize_text(coalesce(req_id, '') || ' ' || coalesce(section, '') || ' '
                             || coalesce(section_path, '') || ' ' || text))
      ) STORED;
    CREATE INDEX IF NOT EXISTS brd_chunk_search_tsv_idx ON brd_chunk USING gin (search_tsv);
  END IF;
END $$;

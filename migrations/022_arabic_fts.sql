-- Arabic-aware keyword search — fixes keyword recall = 0 on Arabic BRDs.
--
-- Two problems the eval harness surfaced (backend/eval/run.py):
--   1. plainto_tsquery ANDs every term, so a paraphrased query never matches a chunk
--      (all tokens must co-occur). We switch to an OR query (partial match, ts_rank-ordered;
--      RRF + Voyage rerank restore precision downstream).
--   2. No Arabic normalization: alef variants (أ إ آ ٱ → ا), alef-maqsura (ى → ي),
--      ta-marbuta (ة → ه), tashkeel diacritics and tatweel differ between a query and the
--      document, so the same word tokenises differently. We normalize BOTH the index and the
--      query with the same function.
--
-- Idempotent: functions are CREATE OR REPLACE; the search_tsv column is only rebuilt when it
-- is not already normalized (so re-running on each deploy does not rewrite the table).
--
-- Apply:  psql "$DATABASE_URL" -f migrations/022_arabic_fts.sql

-- fold Arabic letter forms + lowercase; strip tashkeel (U+064B–U+0652, U+0670) and tatweel.
CREATE OR REPLACE FUNCTION brd_normalize_text(t text) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT lower(
    translate(
      regexp_replace(coalesce(t, ''), '[ًٌٍَُِّْٰـ]', '', 'g'),
      'أإآٱىة',
      'اااايه'
    )
  )
$$;

-- OR tsquery over the normalized query's lexemes: partial match, so a paraphrase still
-- retrieves; ts_rank then orders and the reranker re-scores.
CREATE OR REPLACE FUNCTION brd_or_tsquery(q text) RETURNS tsquery
  LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT to_tsquery('simple', string_agg(lexeme, ' | '))
  FROM (
    SELECT DISTINCT regexp_replace(lexeme, '[^[:alnum:]ء-ي]', '', 'g') AS lexeme
    FROM unnest(tsvector_to_array(to_tsvector('simple', brd_normalize_text(q)))) AS lexeme
  ) toks
  WHERE lexeme <> '' AND length(lexeme) >= 2;
$$;

-- Rebuild search_tsv over normalized (req_id + section + text), only if not already normalized.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'brd_chunk' AND column_name = 'search_tsv'
      AND generation_expression LIKE '%brd_normalize_text%'
  ) THEN
    ALTER TABLE brd_chunk DROP COLUMN IF EXISTS search_tsv;
    ALTER TABLE brd_chunk ADD COLUMN search_tsv tsvector
      GENERATED ALWAYS AS (
        to_tsvector('simple',
          brd_normalize_text(coalesce(req_id, '') || ' ' || coalesce(section, '') || ' ' || text))
      ) STORED;
    CREATE INDEX IF NOT EXISTS brd_chunk_search_tsv_idx ON brd_chunk USING gin (search_tsv);
  END IF;
END $$;

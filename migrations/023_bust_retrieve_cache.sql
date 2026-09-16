-- Bust the retrieve cache on deploy.
--
-- The Arabic keyword fix (migration 022) changed WHICH chunks keyword search returns, but
-- retrieve-cache entries are keyed on the query string + params, not on corpus content — so
-- stale vector-only rankings would otherwise linger until TTL. Clearing the 'retrieve'
-- namespace forces a fresh, cheap recompute: the 'embed' cache (immutable query vectors) is
-- preserved so nothing is re-embedded, and the 'answer' cache self-invalidates because a
-- changed retrieval shifts the chunk_ids in its key.
--
-- Runs via the pre-deploy migration step; safe (and reasonable hygiene) to run on every
-- deploy, since retrieval behaviour can change between deploys. Guarded so it is a no-op if
-- the cache table is absent.
--
-- Apply:  psql "$DATABASE_URL" -f migrations/023_bust_retrieve_cache.sql

DO $$
BEGIN
  IF to_regclass('public.cache_kv') IS NOT NULL THEN
    DELETE FROM cache_kv WHERE namespace = 'retrieve';
  END IF;
END $$;

-- PROTECTED REGENERATION: a requirement change never deletes or rewrites a TESTED use case.
-- See docs/USECASE_TESTING.md §5. Requires 027/028.
--
-- On an incremental sync, an UNTESTED card of a superseded batch is replaced as before
-- (nothing to lose). A TESTED card (any result or proof on its uid) is left untouched and
-- gets a PROPOSAL instead:
--   * kind='modify'   — regeneration produced a matching candidate (aligned by source-
--                       requirement overlap + title similarity); `proposed` holds it and
--                       `diff` the per-field old -> new, for a red/green human review.
--   * kind='obsolete' — regeneration no longer produces this case.
-- Accept applies the candidate IN PLACE (uid kept → every result/proof stays attached; the
-- content hash moves, so the verdict shows "needs retest"), or removes an obsolete card
-- (snapshot first → recoverable in Trash). Dismiss keeps the card as is.
--
-- brd_real only (FK to app_user). Re-runnable.
-- Apply:  psql "$DATABASE_URL" -f migrations/029_use_case_change_proposal.sql

CREATE TABLE IF NOT EXISTS use_case_change_proposal (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    use_case_uid  bigint NOT NULL,                     -- the tested card being protected
    owner_id      int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project       text NOT NULL,
    batch_hash    text,                                -- superseding batch that triggered it
    reason        text,
    proposed      jsonb,                               -- candidate {title, steps, ...} (NULL for obsolete)
    diff          jsonb,                               -- {field: {before, after}}
    kind          text NOT NULL DEFAULT 'modify',      -- modify | obsolete
    status        text NOT NULL DEFAULT 'pending',     -- pending | accepted | dismissed
    created_by    bigint REFERENCES app_user(id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    resolved_by   bigint REFERENCES app_user(id) ON DELETE SET NULL,
    resolved_at   timestamptz
);
-- at most ONE open proposal per card (a repeated sync refreshes it instead of stacking)
CREATE UNIQUE INDEX IF NOT EXISTS use_case_change_proposal_open_uk
    ON use_case_change_proposal(use_case_uid) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS use_case_change_proposal_feed_idx
    ON use_case_change_proposal(owner_id, project, status);

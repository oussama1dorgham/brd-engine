# Use-Case Change Management — versioning, audit & incremental regeneration

Working spec for branch `feat/usecase-versioning-and-incremental-regen`.

## Goal

For **every** use case, **always**: version control, an audit log of what changed, and
full version history kept as a restorable backup — and make **regeneration incremental**
so a requirements change re-derives only the affected use cases, not the whole tree.

## The one invariant

> No use case is ever deleted or overwritten without first recording an immutable
> version row **in the same transaction**.

This single rule delivers versioning, the audit trail, backup/restore, and makes all
regeneration paths non-destructive.

## Scopes

| # | Scope | Delivers |
|---|---|---|
| S1 | Incremental regeneration (`sync`) | Re-derive only use cases from *changed* requirement batches |
| S2 | Per-use-case version history | Full immutable snapshot of every card at every change |
| S3 | Audit log | "What changed" — field diffs derived from consecutive snapshots |
| S4 | Non-destructive regeneration | `replace` / `sync` / `resume` preserve history before deleting |
| S5 | Restore & Trash | Recover any prior version or any deleted/superseded card |
| S6 | API + Frontend | Endpoints + History panel + Trash + "Update" affordance |

## Data model

**`use_case`** gains (migration 020):
- `uid bigint` — stable logical identity (sequence-backed DEFAULT), preserved across
  edits and delete→restore. Unique across live rows.
- `batch_hash text` — the producing batch's `_reqs_hash`, linking a card to its batch.

**`use_case_version`** (migration 021) — full-snapshot history; `owner_id/project/uc_id`
denormalized so history outlives the card; keyed by `use_case_uid`. `change_kind` ∈
`create|edit|move|delete|regenerate|restore`.

**`use_case_tree_snapshot`** (021, optional) — one JSONB row of the whole tree taken
before a full `replace`, for one-click whole-regenerate undo.

## Regeneration workflows

| Mode | Trigger | Deletions | Versions emitted |
|---|---|---|---|
| `resume` | continue a stopped run | none | `create` per new card |
| `sync` (new default "Update") | requirements changed | superseded batches' cards | `regenerate` (pre-delete) + `create` |
| `replace` (Regenerate) | user forces full rebuild | all cards | `regenerate` per card (+ tree snapshot) + `create` |

Regen creates a **new `uid` thread** per new card; superseded/old cards remain in history
and appear in **Trash** as restorable backups (no fuzzy 1:1 lineage in v1).

## Incremental `sync` algorithm

1. `current = {batch_hash for each batch in _batches(reqs)}`
2. `superseded = {batch_hash on live cards ∪ done-ledger hashes} − current`
3. one tx: snapshot (`regenerate`) then delete each superseded card; delete their ledger rows
4. run the existing resume loop — only changed/new batches regenerate; the rest untouched
5. prune empty auto-folders

Blast radius: in-place text edit → 1 batch; split/add/remove → that batch + later batches
in the same scope only; never other scopes.

## Decisions (locked)

1. Stable identity via `use_case.uid`.
2. Retention: keep all versions.
3. Audit: single full-snapshot table, diffs derived on read.
4. Regen identity: new `uid` threads + old cards to Trash.
5. Include the optional whole-tree snapshot.
6. Order: versioning before incremental sync (regen non-destructive from step one).

## Phases

- [x] **Phase 0 — schema:** migrations 020 (identity + batch link) & 021 (version history) + backfill.
- [x] **Phase 1 — versioning core:** `backend/use_case_versioning.py` (capture/record/diff/history/restore/list_deleted); wired create/edit/move/delete atomically in `use_cases.py`.
- [ ] **Phase 2 — non-destructive regen:** reroute `clear()`/`replace` through snapshot-before-delete; tree snapshot.
- [ ] **Phase 3 — incremental `sync`:** supersession diff + prune + `resume_state` count; `mode="sync"`.
- [ ] **Phase 4 — API:** history / diff / restore / deleted; `mode="sync"`; superseded count in `resume_state`.
- [ ] **Phase 5 — Frontend:** History panel, Trash view, "Update" affordance.
- [ ] **Phase 6 — tests:** all paths.

## Apply migrations

```bash
docker compose up -d
psql "$DATABASE_URL" -f migrations/020_use_case_identity.sql
psql "$DATABASE_URL" -f migrations/021_use_case_version.sql
```

## Risks

- Volume: a full Regenerate writes ~N version rows + N new — acceptable (text; regen is
  already the expensive op). Tree snapshot is one row.
- Taxonomy drift on `sync`: a changed req can re-derive a large scope's folder names;
  mitigate by pinning taxonomy or accept minor drift.
- Concurrency: supersession delete + regen reuse the ledger's atomic per-batch locking;
  snapshot-before-delete under `FOR UPDATE` avoids races.
- Backfill: existing cards get a fresh `uid` and a synthetic `create` version so every
  card has history from day one.

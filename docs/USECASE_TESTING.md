# Use-Case Test Status, Proofs & Protected Regeneration — Design & Review

> **Superseded in part (2026-09):** protected regeneration — change proposals (§4 “Migration 028 — proposal queue”,
> §5, the proposal parts of §12) — is retired. Use cases are now version-controlled: the live cases are a draft,
> publishing freezes an immutable version, cycles run a version, and Update changes only affected cases in place.
> See **`docs/USECASE_VERSIONS.md`**. The status / evidence / cycle / audit model below still applies.

Pre-implementation analysis for the branch `feat/use-case-test-status`.
Scope: give every use case a **test-execution lifecycle** (status + evidence + history),
and make requirement-driven **regeneration non-destructive** for any use case that has been
tested — a human accepts the change; the tested card, its status, and all its proofs are
never silently overwritten.

Read alongside `docs/USECASE_VERSIONING.md` (the definition-history engine this builds on).

---

## 1. Why this is needed / what exists today

The use-case tree (`use_case`, `use_case_folder`) is generated from a BRD, editable, and
**version-controlled at the definition level**: every edit/delete/move/regenerate records an
immutable full snapshot in `use_case_version`, keyed by a stable `uid` that survives a
delete→restore (see `backend/use_cases/versioning.py`, migrations 018/020/021).

Two gaps:

1. **No execution state.** A use case is a *test case*, but there is nowhere to record whether
   it was run, whether it passed, or the evidence. The existing `use_case.status`
   (`draft | approved`, migration 018) is an **authoring lifecycle**, not a test result —
   overloading it would break approval and pollute the definition-version diffs.
2. **Regeneration is destructive.** After a requirement changes, `sync` mode supersedes the
   affected batch: it snapshots→**deletes**→regenerates the cards into **new uids**
   (`_sync_prune`, `backend/use_cases/generation.py`). Any execution state attached to a card
   would be stranded on the old (Trashed) uid. For a *tested* card that is unacceptable.

### The two orthogonal dimensions (the framing that drives the whole design)

| Dimension | Column / store | Meaning | Owner |
|---|---|---|---|
| Authoring lifecycle | `use_case.status` (`draft`/`approved`) | is the *definition* ready | existing |
| **Test execution** | `use_case.test_status` + `use_case_test_result` | result of *running* the test | **new** |

They are independent: a card can be `approved` **and** `failed`. Execution state is **never**
added to the definition version snapshot (`_FIELDS` in `use_cases/versioning.py`) and is **never**
rolled back by a definition restore.

---

## 2. Guiding principle

> **A tested card is immutable to automation.**
> Only a human edit or an *accepted* proposal changes its definition, and it always keeps its
> `uid`. Because every result and proof is keyed to that `uid`, execution history survives any
> number of requirement changes. Sync stops deleting tested cards — it becomes a *proposal
> generator* for them.

Everything below follows from that one rule.

---

## 3. Status model — one axis, not a flat list

A status set must be a set of **peers**: every option answers the *same* question. An earlier
draft mixed four different questions (progress vs. outcome vs. staleness vs. applicability) into
one pick-list — which is why it felt scattered. The organized model is **one primary axis plus
two secondary signals that are deliberately NOT statuses**:

**Primary — execution lifecycle** (`use_case.test_status`), mutually exclusive, one canonical
order used everywhere (enum, buttons, filter — no re-ordering):

| Status | Group | Meaning |
|---|---|---|
| `not_tested` *(default)* | pending | never run; every new/regenerated card starts here |
| `in_progress` | pending | actively being executed |
| `blocked` | unresolved | attempted but can't complete (env/dependency/blocking bug) |
| `failed` | resolved | ran, did not meet expected behaviour |
| `passed` | resolved | ran, met expected behaviour |

`not_tested → in_progress → blocked → failed → passed`. The colour language falls out of the
grouping: pending = grey, blocked = amber, failed = red, passed = green.

**Secondary 1 — needs-retest (DERIVED, never a status).** A recorded verdict whose
`tested_against_ver` is older than the card's current definition version is stale. Computed on
read and shown as a **badge over the last verdict** (so "passed ⚠ needs retest" keeps the
verdict instead of collapsing to a meaningless bare state) plus a cross-cutting filter. No
column — it's `tested_against_ver < max(use_case_version.version_no)`.

**Secondary 2 — scope (SEPARATE axis).** `use_case.in_scope` answers "does this test apply?",
not "what happened when we ran it". An out-of-scope card is **excluded from coverage math** (it
is not a verdict), toggled via an auditable `scope` timeline event. This replaces the earlier
`na` catch-all, which polluted the pass-rate by masquerading as an outcome.

---

## 4. Data model

### Migration 027 — execution (status + proofs)

```sql
-- fast denormalized CURRENT status + scope axis for tree render + filter (mirrors case_type)
ALTER TABLE use_case ADD COLUMN test_status text NOT NULL DEFAULT 'not_tested';
ALTER TABLE use_case ADD COLUMN in_scope    boolean NOT NULL DEFAULT true;   -- scope axis (§3)
CREATE INDEX use_case_test_status_idx ON use_case(owner_id, project, test_status);

-- one row per timeline EVENT, keyed by the stable uid (survives delete->restore)
CREATE TABLE use_case_test_result (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  use_case_uid        bigint NOT NULL,               -- use_case.uid (migration 020)
  owner_id            int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  project             text NOT NULL,
  event_kind          text NOT NULL DEFAULT 'status',-- status | note | scope
  status              text NOT NULL,                 -- lifecycle enum (carried fwd on note/scope)
  note                text,
  in_scope            boolean,                       -- only on a 'scope' event: value scope was set TO
  tested_against_ver  int,                           -- use_case_version.version_no at record time
  tested_by           bigint REFERENCES app_user(id) ON DELETE SET NULL,
  tested_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX use_case_test_result_uid_idx  ON use_case_test_result(use_case_uid, tested_at DESC);
CREATE INDEX use_case_test_result_feed_idx ON use_case_test_result(owner_id, project, tested_at DESC);

-- proofs bound to the EVENT that produced them, so evidence stays with "the failed"
CREATE TABLE use_case_test_proof (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  result_id     bigint NOT NULL REFERENCES use_case_test_result(id) ON DELETE CASCADE,
  use_case_uid  bigint NOT NULL,                     -- denormalized: "all proofs for this card"
  owner_id      int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  kind          text NOT NULL,                       -- image|video|file|link  (inline text = event note)
  filename      text, mime text, size int,
  content       bytea,                               -- blob (NULL when kind in link/note)
  url           text,                                -- external link (NULL when blob)
  uploaded_by   bigint REFERENCES app_user(id) ON DELETE SET NULL,
  uploaded_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX use_case_test_proof_uid_idx ON use_case_test_proof(use_case_uid);
```

**Behaviour:** set a card to `failed` → a `failed` result row opens → attach N proofs to *that*
row → later set `passed` → a new `passed` row opens with its own proofs. The failed event and
its evidence are frozen in the timeline forever. Multiple proofs per card, per event, all
retrievable. The denormalized `use_case.test_status` = the latest result's status (or
`not_tested`), kept only for fast tree render/filter.

### Migration 028 — proposal queue (protected regeneration)

```sql
CREATE TABLE use_case_change_proposal (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  use_case_uid  bigint NOT NULL,                     -- the tested card being protected
  owner_id      int  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  project       text NOT NULL,
  batch_hash    text,                                -- superseding batch that triggered it
  reason        text,                                -- e.g. "Requirement REQ-14 changed"
  proposed      jsonb NOT NULL,                      -- candidate {title, steps, expected_behaviour, ...}
  diff          jsonb,                               -- precomputed field-level old->new (red/green)
  kind          text NOT NULL DEFAULT 'modify',      -- modify | obsolete (regen dropped this card)
  status        text NOT NULL DEFAULT 'pending',     -- pending|accepted|dismissed
  created_by    bigint REFERENCES app_user(id) ON DELETE SET NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  resolved_by   bigint REFERENCES app_user(id) ON DELETE SET NULL,
  resolved_at   timestamptz
);
-- at most ONE open proposal per card (idempotent re-sync)
CREATE UNIQUE INDEX use_case_change_proposal_open_uk
  ON use_case_change_proposal(use_case_uid) WHERE status='pending';
CREATE INDEX use_case_change_proposal_feed_idx
  ON use_case_change_proposal(owner_id, project, status);
```

All three tables are **brd_real only** (FK to `app_user`), like 018–026.

---

## 5. Regeneration behaviour (the core change)

Today `sync` (`generate_for_project(mode="sync")` → `_sync_prune`) deletes superseded cards and
regenerates them into new uids. New path: regenerate candidates **into memory first**, then
partition each superseded batch's existing cards by execution state.

```
for each superseded batch:
    candidates = regenerate(batch)                # LLM, NOT applied yet
    for card in batch.cards:
        if card is UNTESTED (test_status='not_tested' AND no results AND no proofs):
            apply as today  (snapshot -> delete -> insert fresh)   # nothing to protect
        else:                                                       # TESTED -> never touched
            cand = align(card, candidates)         # match, don't mutate
            open/refresh a use_case_change_proposal(card.uid, proposed=cand, diff=..., reason)
    unmatched candidates -> insert as genuinely new cards
    mark the batch ledger 'done' with the new hash # so sync doesn't re-fire
```

- **Untested cards** keep the current fast, destructive behaviour — there is no evidence to lose.
- **Tested cards** are flagged (a join to pending proposals in `get_tree`), **not mutated**.
  Their status and proofs stay valid against the still-live definition.
- **Alignment** (`_align`): match a tested card to its candidate by **`source_chunk_ids`
  overlap** (both cite requirement chunks — strongest signal), tie-broken by title similarity.
  - matched → proposal `kind='modify'`
  - tested card the regeneration dropped → `kind='obsolete'`
  - candidate matching nothing → inserted as a new card (an addition)
- Idempotent: the `WHERE status='pending'` unique index means a repeated sync refreshes the open
  proposal rather than stacking duplicates.

### Accept / Dismiss (the red/green human gate)

Review modal renders `diff` as **old = red, proposed = green, per field**, reusing the
field-diff shape of `ucv.diff` and the requirements-changes modal already in the UI.

- **Accept (`modify`)**
  1. Update the card's definition **in place** — `uid` preserved → every result and proof stays
     attached.
  2. `ucv.record_change(cur, uc_pk, "regenerate", …)` records the new definition as a version;
     the prior definition becomes the previous version — retrievable and restorable.
  3. Definition moved → **auto-flip to `retest`**: write `use_case_test_result(status='retest',
     tested_against_ver=<new version_no>, note='definition updated by accepted requirement
     change')`. The old passed/failed result **and its proofs remain earlier in the timeline**,
     untouched. New testing opens new events with new proofs.
  4. Proposal → `accepted`; relink `use_case.batch_hash` to the new hash.
- **Accept (`obsolete`)**: user confirms removal → normal snapshot→delete (recoverable in Trash
  with full history). The **only** way a tested card leaves is human-initiated.
- **Dismiss**: proposal → `dismissed`, card untouched. It knowingly diverges from the reworded
  requirement until the next change re-proposes.

---

## 6. Requirement → mechanism map

| Requirement (from the user) | Mechanism |
|---|---|
| Multiple test proofs on one test case | `use_case_test_proof`, N per event, across all events |
| Status updatable with history saved | append-only `use_case_test_result` timeline |
| Update to failed keeps evidence *on the failed* | proofs bound to `result_id`, not to the card |
| History of all statuses across the lifecycle | the result timeline **is** the lifecycle |
| On a requirement change, tested card stays untouched, only marked | proposal queue; sync never deletes tested cards |
| Change text red (old) / green (new), user accepts | `diff` jsonb → review modal → Accept/Dismiss |
| All status-update flows still usable | card stays live and editable throughout |
| Old kept in history with all proofs, retrievable anytime | `uid` preserved → results/proofs persist; old definition in `use_case_version` |

---

## 7. Code surface

**New modules**
- `backend/use_cases/testing.py` — `set_status`, `add_proof`, `list_proofs`, `result_timeline`,
  `report`. `set_status` writes a result row + updates denormalized `use_case.test_status`, and
  **does not** call `ucv.record_change` (execution ≠ definition edit).
- `backend/use_cases/proposals.py` — `open_proposal`, `_align`, `_field_diff`, `list_pending`,
  `accept`, `dismiss`.

**Modify**
- `backend/use_cases/generation.py`
  - `generate_for_project` sync branch (~L577) + `_sync_prune` (~L443) → partition/propose logic.
  - `get_tree` (~L663) → add `test_status` + `has_pending` (join pending proposals).
  - `_write_batch` (~L408) → new cards default `test_status='not_tested'`.
  - **Do not** route `test_status` through `update_use_case`, and **do not** add it to `_FIELDS`
    or the restore UPDATE in `use_cases/versioning.py`.
- `backend/api.py` — new endpoints near L1321:
  - `POST /use-cases/test-status`  `{id, status, note}`
  - `POST /use-cases/proof`        (multipart: id/result, file|url, kind)
  - `GET  /use-cases/{uid}/results` (timeline + proofs)
  - `GET  /use-cases/proposals?project=` (pending list → tree badges)
  - `GET  /use-cases/proposals/{uid}` (the diff)
  - `POST /use-cases/proposals/accept`  `{uid}`
  - `POST /use-cases/proposals/dismiss` `{uid}`
  - `GET  /use-cases/report?project=` (+ export)

**Frontend** (`frontend/src/components/use-cases/UseCasesPage.tsx`, API client, styles)
- colored test-status pill + quick-set on each card (reuse the case_type tag pattern)
- detail panel: status timeline (event → status/who/when/note + its proof gallery) + multi-upload
- "requirement changed — review" badge on cards with a pending proposal
- red/green Accept/Dismiss modal (reuse the requirements-changes modal)
- `test_status` filter chips (mirror the case_type filter)
- Reports view + export (xlsx/pdf skills)

**Tests** — extend `tests/` (pure logic): status enum validation, `_align` matching,
`_field_diff`, proposal accept/dismiss state machine, report aggregation.

---

## 8. Reports (Phase 3)

`GET /use-cases/report?project=` aggregates `use_case` ⨝ latest `use_case_test_result`:
- execution summary (counts by status, overall + per folder/scope)
- coverage by `case_type` (functional / negative / edge pass rates)
- **requirements traceability** via `source_chunk_ids`: per requirement, are its use cases
  passing / failed / blocked / invalidated by a change
- retest backlog (definition or requirement moved past last result)
- failures-with-evidence list (proof links) for triage handoff

Export through the xlsx / pdf / docx skills. *Named test runs/cycles* (point-in-time,
build-comparable reports) are deferred — flagged as a future scope lever, not v1.

---

## 9. Edge cases & invariants

- **Definition restore never changes test status** — execution state is not in the version
  snapshot. Guard: `test_status` stays out of `_FIELDS` and the restore UPDATE.
- **Delete a tested card** → results/proofs persist under the uid (keyed by uid, like versions);
  restore from Trash re-inserts the same uid and reconnects them.
- **Blast radius of a requirement change is the whole batch** (up to `_SCOPE_BATCH` requirements'
  cards), not one card — so one edit can raise several proposals. Expected.
- **Pre-feature cards (`batch_hash` NULL)** are immune to sync; a full **Replace** still wipes
  the tree — Replace remains a destructive, explicit user action (document the warning; consider
  guarding Replace when tested cards exist).
- **Proof storage v1** — DB `bytea` with a size cap (≤5 MB) + always-allowed external URL. Move
  to disk/object store when video demand appears; the `content`/`url` split makes that painless.
- **Proofs attach to the current (latest) result event** until the status changes, at which point
  a new event opens.
- **No RAG-cache impact** — `cache.py` namespaces (`embed`/`retrieve`/`answer`) cover chat only;
  the use-case tree is not cached.
- **Concurrency** — proposal accept/dismiss and status writes run in the caller's transaction,
  owner-scoped; the `WHERE status='pending'` unique index prevents duplicate open proposals.

---

## 10. Phasing

1. **Execution model** — migration 027, `use_cases/testing.py`, status/proof endpoints, UI pill +
   timeline + multi-upload + filter. *Ships the full status lifecycle with per-status proofs,
   standalone and independently useful.* **Done.**
2. **Test cycles, hardening, dynamic flow** — migration 028 (see §13). **Done.**
3. **Protected regeneration** — migration **029** (the proposal table sketched in §4 as "028"),
   `backend/use_cases/proposals.py` (`align`, `field_diff`, `propose_in`, `resolve`), sync rewrite
   in `use_cases._sync_prune` / `_protect_batch` / `_protect_leftovers`, accept/dismiss endpoints
   + red/green review modal (`ProposalReview.tsx`). **Done** (per-field accept / editable green
   side from §12 still deferred).
4. **Reports + export** — folded into §13 (coverage + traceability report, CSV export). **Done.**

Phase 1 must land first: sync can only "protect tested cards" once *tested* is a detectable
state (`test_status` / results / proofs).

---

## 11. Open confirmations before Phase 1 coding

1. **Proof storage** — DB `bytea` + ≤5 MB cap + external URL for v1? (recommended)
2. **Proofs-on-current-status** — keep adding proofs to the latest status event until status
   changes, then a new event opens? (recommended)
3. **Status enum** — accept the 7 in §3, or trim/rename any?
4. **Untested-card sync** — auto-apply regeneration for untested cards (current behaviour), or
   route *every* affected card through the proposal queue for one consistent flow?

> **Resolved:** all four accepted — DB `bytea` ≤5 MB + external URL; proofs bind to the current
> event; untested cards auto-apply on sync. Status model later **reworked** (§3): one 5-status
> lifecycle, retest demoted to a derived badge, `na` replaced by a separate `in_scope` axis.

---

## 12. Dynamic UX enhancements (freedom-first)

Goal: proof capture and status logging should feel fluid — the tester is never forced through
ceremony to record what they see. The schema deltas here **amend §4**.

### Schema deltas
- `use_case_test_result` **+ `event_kind text NOT NULL DEFAULT 'status'`** — `status` (a verdict
  change), `note` (a comment/log entry, no verdict) or `scope` (an in/out-of-scope toggle). Makes
  the timeline a full **activity log** and lets evidence be logged before any verdict.
- `use_case_test_proof` **+ `caption text`** and **`ordinal int NOT NULL DEFAULT 0`** — labelled,
  reorderable proofs.
- **Extensible status validation** — validate `status` against *(built-in set ∪ per-project custom
  defs)*, not a frozen Python enum. Ship the **5 lifecycle** built-ins now (§3); a later
  `use_case_test_status_def(owner_id, project, key, label, color, semantic)` table adds custom
  statuses with **no migration churn** — reports key off `semantic` (`pass|fail|block|neutral`)
  so custom labels never break aggregation.

### Proofs — capture freedom  *(Phase 1)*
- **One uploader, all inputs:** drag-drop, multi-file select, **clipboard paste** (screenshots),
  **URL paste** (Jira/CI/video), and **inline markdown note**. Kinds mix freely on one event.
- **Add proofs any time, verdict or not:** the first proof/note on a `not_tested` card opens a
  `note` event to bind to — no forced pass/fail just to attach evidence.
- **Per-proof caption + drag-reorder.** Freely delete/replace on the **latest (open) event**;
  proofs on **past events are locked** (immutable evidence) but always viewable.

### Status / timeline — logging freedom  *(Phase 1)*
- **Log a result in one gesture:** status + optional note + dropped proofs together — or just flip
  the status bare. Both allowed.
- **Timeline = activity log:** `note` events (blockers, observations, links) interleave with
  verdicts.
- **Correct-the-last-event:** edit the latest event's status/note/proofs in place to fix a mislog,
  instead of forcing a new row; older events stay immutable.
- Quick-set from the **tree pill**, the **detail panel**, or **keyboard shortcuts**.
- **Bulk set:** multi-select cards → one status / one shared link to all.  *(Phase 1 if cheap,
  else early Phase 2)*

### Review / proposal — decision freedom  *(Phase 2)*
- **Per-field accept:** take the new title, keep the old steps — the diff is already per-field, so
  each field gets its own accept toggle. `accept` takes optional field overrides; full-accept is
  the default.
- **Editable green side:** tweak the proposed text before accepting, not just take it verbatim.
- **Batch review:** one modal lists every pending proposal for the project; accept/dismiss/bulk.
- **Dismiss with reason / snooze.**

### Deferred (freedom, later)
Custom-status **UI**, image annotation/markup, proof reuse across cards, tester
assignees/reassignment. *(Named test runs/cycles shipped — §13.)*

---

## 13. Round 2 — cycles, content-hash retest, evidence staging, hardening (migration 028)

Driven by a review of Phase 1 (B1–B6 + security + perf + UX). Everything below is live.

### Retest clock = content hash (fixes B1)
`use_case.def_hash` is a **generated** column: `md5(preconditions, steps, expected_behaviour,
roles, source_chunk_ids)` — only fields that change what a tester does. Every write path (edit,
restore, regenerate, accepted proposal) keeps it current for free. A verdict pins
`use_case_test_result.tested_against_hash`; `use_case.tested_hash` caches the current verdict's pin.
`needs_retest = test_status ∈ {blocked, failed, passed} ∧ tested_hash ≠ def_hash` — a plain
column compare (the tree query no longer joins results/versions). A move, rename, approve, no-op
save or delete→restore no longer trips it; restoring the exact tested definition clears it.
Pre-028 results were backfilled as "tested against the current definition".

### One source of truth for the cache (fixes B2, B6)
`use_case.test_status / tested_hash / in_scope` are recomputed by **`resync_card`** from the
timeline after every write/undo — and by `use_case_versioning.restore`, so a card re-inserted
from Trash comes back with its verdict. Every write locks the card row (`FOR UPDATE`).

### Evidence staging (fixes B5)
`use_case_test_proof.result_id` is nullable: an upload is **staged** (pending) and the next
verdict or note **claims** it — screenshot first, verdict second lands evidence on the right
event. Attachments alone can be logged as a note ("Log evidence"). No more empty auto-note
events. Undoing a fresh event returns its proofs to staging (and a note's text to the composer).

### Amend + undo (replaces the "fix…" select)
A re-verdict by the same tester, in the same cycle, within `AMEND_WINDOW_SECONDS` (300) **amends**
the latest verdict in place (`amended_from` keeps what it replaced). The latest entry is
**undoable** inside the window: an amend reverts, a fresh event is removed. Older events stay
immutable; the latest event's note/defect is editable.

### Test cycles (traceability)
`test_cycle(name, description, status open|closed)` + `test_cycle_case(cycle_id, uid,
planned_hash)` (planned membership, pinned to the definition it was planned at). Results carry
`cycle_id`. The active cycle (per project, remembered in the browser) overlays **its** statuses on
the tree and receives every verdict; recording on an unplanned case plans it in. A closed cycle is
read-only; deleting a cycle keeps its results as ad-hoc runs. Creation plans cases from
`all | not_passed | failed_blocked | retest | empty`, narrowed by case type / folders / a previous
cycle ("re-run what failed in Sprint 12").

### Defects, bulk, keyboard, report
- Failed/Blocked open an optional **reason + defect** prompt (`defect_ref` on the event).
- **Bulk:** Ctrl/⌘/Shift-click (or ☑ on a folder) → verdict / scope / plan into or out of the cycle.
- **Keys:** P/F/B/I verdicts, N next not-run, J/K move, Esc clears selection; optional
  auto-advance after a verdict.
- **Report** (`GET /use-cases/report?project=&cycle_id=`): KPIs, segmented progress, requirement →
  cases → verdict matrix with a coverage state per requirement (`failed | blocked | uncovered |
  retest | partial | not_run | passed`), defects, case-type breakdown; Cases + Traceability CSV.

### Security + performance
- Proof kind is derived from a sanitized mime server-side (SVG = file); filenames stripped of
  paths/control chars; RFC 5987 `Content-Disposition`.
- Only passive media (png/jpeg/gif/webp/bmp/avif, mp4/webm/ogg/mov, pdf, text/plain) is served
  inline; everything else downloads as `application/octet-stream`. All blobs: `nosniff`, a
  `sandbox` CSP (except PDF), `Cache-Control: private, max-age=31536000, immutable`.
- Link proofs: absolute `http(s)` only (no `javascript:`/`data:`), ≤2048 chars.
- Uploads are refused on `Content-Length` and capped while streaming (413), never buffered whole.
- Writes return `{event, card}` — the UI patches the timeline + tree in place (no refetches).
- BRD deletion purges results, proofs (blobs), cycles and proposals (fixes B3).

### Endpoints (round 2)
`POST /use-cases/test-status` (record/amend; claims staged) · `/test-status/bulk` ·
`/test-status/undo` · `/test-status/edit` · `POST /use-cases/scope` · `/scope/bulk` ·
`POST /use-cases/proof/link|file` (stage) · `GET /use-cases/proof/{id}[?download=1]` ·
`GET /use-cases/{uid}/timeline?cycle_id=` · `GET /use-cases/report?project=&cycle_id=` ·
`GET /test-cycles?project=` · `POST /test-cycles/create|update|delete|members` ·
`GET /test-cycles/{id}/statuses` · `GET /use-cases/proposals?project=` ·
`POST /use-cases/proposals/resolve`.


---

## 14. Audit log + run workspace (migration 030)

### Per-use-case audit log
`use_case_audit` is the **complete, attributable, append-only** history of each use case — one row
per act: `actor_id` + denormalized `actor_email`, `created_at`, `category`
(`definition | test | evidence | scope | cycle | proposal`), `action`, a human `summary`,
`status_before → status_after`, `cycle_id/cycle_name`, `version_no` (definition entries),
`result_id` (test entries) and a `detail` payload (note, defect, attachments, previous note…).
`source` is `user` or `regeneration` (the generation job acting for the user who started it).

- **Why a separate table:** the timeline is working state — an in-window re-verdict *amends* its
  row, an undo *deletes* it, a proof removal deletes the proof. The audit keeps the trace of every
  one of those acts (`status_amended`, `status_reverted`, `entry_undone`, `proof_removed`, …).
- **Append-only:** a DB trigger rejects `UPDATE`; rows are deleted only by a BRD purge.
- **Atomic:** `use_case_audit.log(cur, …)` runs in the caller's transaction — a change and its
  audit entry commit together. Definition changes are logged centrally in
  `use_case_versioning._record`; test/evidence/scope/cycle acts in `use_case_testing`; proposals
  in `use_case_proposals`. Edit/delete/move/folder-delete now carry the acting user
  (`changed_by`) end to end.
- **Backfilled** from `use_case_version`, `use_case_test_result` (with the previous verdict via
  `lag`) and `use_case_test_proof`.
- `GET /use-cases/{uid}/audit` → entries (newest first) + per-category counts. `get_tree` and every
  write's card payload carry `last_by / last_at / last_summary` ("last changed by").

### Run workspace (UI)
The detail pane is a workspace with tabs — **Overview** (default: understand the case first; opening a case from the list lands here) · **▶ Run** (run-flow navigation N / J / K keeps it) · **Audit log** — plus
**⤢ Focus** (hides the list, full-width) and ‹ n / N › / "Next not run" navigation.
- **Run** = two columns: the **test script** (role, preconditions, a step **checklist** — ✓ per
  step, ✕ fails the case at that step and opens the reason prompt pre-filled with the step; all ✓
  offers "record Passed"; the expected result) beside the sticky **execution** panel.
- **Audit log** = day-grouped timeline with avatar, actor, status transition, cycle, detail;
  category filters; definition entries expand to the field diff with **Restore**; CSV export.
  Replaces the old History panel (`UseCaseHistory.tsx` removed).
- The case header shows **last change · by whom · when** (click → audit log).

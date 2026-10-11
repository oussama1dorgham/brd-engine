# Version-Controlled Use Cases — Design & Operations

Use cases are managed like source code: the live cases are one editable **draft**; publishing
freezes the draft into an immutable, numbered **version**; every **test cycle** runs one version;
any two versions — and any two runs — can be compared. This replaces *protected regeneration*
(change proposals), which is retired (see `USECASE_TESTING.md` for the testing model it sits on).

```
 BRD requirements ──(Update: only what changed)──► DRAFT (live use_case rows)
                                                     │  review the diff, publish
                                                     ▼
                                   v1 ─► v2 ─► v3 …  (immutable; restore = new version)
                                   │      │
                              cycle A   cycle B  (a run tests exactly one version;
                                          ▲      passes of unchanged cases carry over)
                                          └── compare A ↔ B: regressions / fixes / cause
```

## 1. Model

| Concept | Where | Notes |
|---|---|---|
| Draft | `use_case` rows | Edits, Update, Trash and per-card restore all change the draft only. |
| Version | `use_case_suite_version` | Number, label (editable), BRD revision it was built on, counts, author. Never deleted while a run uses it. |
| Content | `use_case_content` | One row per **unique** case content (content-addressed: `basis_hash`). Unchanged cases share rows across versions. Immutable. |
| Membership | `use_case_suite_member` | version ⇄ content + folder path + position. A move is a new version with the same content. |
| Cited requirement text | `use_case_req_text` | Stored once per unique text — a version always shows its requirements as they were. |
| Requirement basis | `use_case_req_basis` | md5 of each requirement's text the cases were last generated from (drives Update). |
| Test basis | `use_case.test_basis` | md5(test-relevant fields + cited requirement texts), trigger-maintained. Every verdict pins one. |

Hashes: `content_hash` (every field → "modified"), `def_hash` (test-relevant fields), `test_basis` /
`use_case_content_basis()` (test fields + requirement texts → "needs retest"). One SQL function builds
a version (`use_case_snapshot`), one reads the draft (`use_case_draft_rows`), one hashes cited
requirements (`use_case_req_hashes`) — the diff you review is exactly what publish freezes.

## 2. Flows

* **Update** (`generation.generate_for_project`) — per requirement: new → generate, changed → the model
  answers keep / update / remove for each case citing it (updates **in place**: same uid and code),
  removed → cases citing only removed requirements are removed without an LLM call. Each batch commits
  with its basis rows; a failed batch stays pending and the next Update continues. Codes are never reused.
* **Publish** (`suites.publish`) — refused while generation runs (409) and when the draft changed after
  you reviewed it (stale fingerprint → 409, the dialog reloads). An unchanged draft publishes nothing.
  Every changed case gets an audit entry "Published in vN — modified (steps)".
* **Restore** (`generation.restore_version`) — the draft becomes vN (cases rewritten in place, deleted
  ones re-inserted with their uid, code and test state); publish it as a new version. "Discard
  unpublished changes" = restore the latest version. Requirements edited since vN go back to "changed"
  in the basis, so Update refreshes exactly the restored cases they affect.
* **Cycles** (`testing.create_cycle`) — always run the latest version (one must exist); cases added to the
  draft since are left out (reported). A verdict in a cycle pins **that version's** definition; a case not
  in it can't be recorded there. A cycle on an older version stays usable ("v3 available").
* **Carry** — a new cycle can start each case whose latest verdict is a *pass on the identical definition*
  as Passed, marked *carried*, attributed and audited, pointing at the original result (and its
  evidence). Failed / blocked never carry. Off = full regression.
* **Reports** (`testing.report`) — a cycle's report is built from its version (definitions, folders,
  requirement text then, flagged if changed/removed since); every report shows its version and what
  changed since the previous one. **Compare runs** (`testing.compare_cycles`): regressed / new failure /
  still failing / fixed / newly passing / not re-run / dropped, with the cause — same definition
  (likely the product) or definition/requirement changed (the test may need updating).

## 3. Evidence storage

Proof files live in the object store (`use-case-proofs/<owner>/<random>`, same MinIO/S3 as BRD sources);
the object is written before its row, deleted after the row's delete commits, and `gc_proof_objects`
sweeps anything unreferenced. If the store is down at upload the bytes stay in Postgres; the startup
task `move_blobs_to_storage` moves them (and older uploads) later.

## 4. Gap map (analysis → where it is closed)

| Gap | Closed by |
|---|---|
| G1 identity across regenerations | in-place updates by ref (`incremental.py`), stable uid/code |
| G2 carry-forward | `_carry_passes` (passes on identical basis only) |
| G3 requirement changed under an unchanged case | `test_basis` + triggers on `brd_chunk` / archive |
| G4 manual edits | draft = live rows; publish freezes |
| G5 concurrency | advisory lock per BRD, review fingerprint (409), no publish/restore during generation |
| G6 runs across versions | cycles bound to a version; outdated badge; refusal for cases not in it |
| G7 self-contained versions | requirement texts + BRD revision per version; `case_in_version` |
| G8 BRD changed | per-requirement Update counts; version menu warns on BRD revision drift |
| G9 evidence storage | object store + fallback + migration + GC |
| G10 storage growth | content-addressed rows shared by versions |
| G11 durable generation | per-batch atomic commits with the basis |
| G12 deletes | immutability triggers, RESTRICT (runs → versions), full purge on BRD delete incl. objects |
| G13 codes | `_next_uc_number` over live + history |
| G14 existing data | v1 baseline per BRD; results linked where exact |
| G15 audit | `version` category, `suite_version_id` on entries |
| G16 reports / regressions | versioned reports, run comparison + CSV |
| G17 numbering / restore | labels, restore-as-draft, discard changes; proposals retired |

## 5. Migrations & tests

`032` versions · `033` requirement basis · `034` test basis + carried results · `035` proof objects
(`029_use_case_change_proposal` stays for history; nothing writes it).

Integration suites (sandbox BRDs, `RUN_INTEGRATION=1`): `test_suite_versions`, `test_incremental_generation`,
`test_version_runs`, `test_version_reports`, `test_versions_hardening`, plus the testing / HTTP / audit suites.
Unit: `test_suites_diff`, `test_incremental`, run-change classification in `test_use_case_testing`.

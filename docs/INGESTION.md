# BRD Ingestion v2 — Design & Operations

How an uploaded BRD becomes searchable requirements, how a **new version** of a BRD is
compared and applied, and — above all — **how deletes are handled**. Migration
`031_ingest_v2.sql`; code in `backend/ingest/jobs.py`, `backend/ingest/{extract,parser,
chunker,revise,archive}.py`, `backend/core/storage.py`, `backend/core/voyage.py`. The CLI
entry point (`backend/ingest/pipeline.py`) for a `.pdf`/`.docx` goes through the same
`jobs.submit()` as the UI upload — see its module docstring.

---

## 1. Pipeline

```
POST /upload ─► submit()  validate (content sniff, size, per-user rate, title clash,
                          one active job per BRD) ─► object storage ─► ingest_job (queued)
ingest worker ─► extract ─► parse_text ─► build_chunks ─► embed (cache) ─► store | compare
```

| Stage | What it does |
|---|---|
| **extract** (`ingest/extract.py`) | Validates by **content** (PDF signature / Word package, zip-bomb guard). DOCX → Markdown using the document's own heading styles, lists, tables (vertical merges not duplicated), content controls, no TOC; tracked insertions kept, deletions not. PDF → Markdown with font-size headings, table detection, running header/footer removal, RTL + two-column reading order, `<!-- page:N -->` markers. NFKC normalization (Arabic presentation forms fold to letters). Scanned pages are reported as job warnings (OCR not configured) — shown per-BRD in Manage BRDs. |
| **parse_text** (`ingest/parser.py`) | Headings → sections with `section` (top-level scope), `section_path`, `page`, requirement id (`FR-14`, `NFR 3`, `REQ-12.3`, `US-7`…). |
| **build_chunks** (`ingest/chunker.py`) | **One requirement per chunk**: a section is cut at every paragraph starting with a requirement id (lead-ins / lists stay with it); tables fan out one row per chunk with the header; oversize text windows. |
| **embed** | Input = `"<BRD title> › <section path>\n\n<text>"` (context for short rows); stored text stays the requirement alone. Content-addressed per owner in `brd_embedding_cache`, committed per batch ⇒ a retry **resumes**, a re-upload **re-embeds only changed requirements** — still true after a rename (the context title is kept stable) and edited requirements now carry a matching `embed_hash` so an edit doesn't force a spurious re-embed on the next upload. Ingest priority (see §4). |
| **store** (new BRD) | One transaction; vectors copied server-side from the cache. |
| **compare** (new version) | Stages a `brd_revision` for review — the live BRD is untouched. |

Durability: jobs are claimed with `FOR UPDATE SKIP LOCKED` (any number of processes), a
15-minute lease reclaims a crashed worker's job, transient failures (Voyage, storage, DB)
retry with backoff (1 / 5 / 15 / 30 min) up to `INGEST_MAX_ATTEMPTS`; a bad document fails at
**once**, no retry — extraction raises `extract.ExtractError` (unreadable/corrupt file, zip
bomb, failed quality gate) and the job goes straight to `failed` with its reason
(`brd_document.last_error`, shown in Manage BRDs with **Retry**).
Startup no longer fails in-flight BRDs — only orphans with no job.

**Arabic (RTL) PDFs.** PyMuPDF returns RTL text in the PDF's *visual* order (table cells
reversed character by character, lam-alef pairs swapped: `االستخدام`), which made a whole BRD
unanswerable ("The BRDs do not specify this"). When the first pages are RTL, extraction goes
through **pypdf** (logical order) instead: a text visitor collects font sizes, repeated
edge lines / page numbers are dropped, and heading levels come from the document's structure
(the title size seen only on page 1 → `#`; sizes used on ≥3 pages whose lines are mostly
numbered `1.2 …` → `##`…). Body lines starting with `#` (a table's "#" column) are escaped so
they never become headings.

**Quality gate.** After extraction, an RTL document whose Arabic words look visual-order
(`visual_order_ratio` > 5 %) is rejected with a clear reason ("upload the Word version")
instead of being ingested. Correct extraction measures ≈0.5 %; the broken one was ≫5 %.

## 2. New versions (compare → apply or discard)

Uploading a file whose title matches an existing BRD answers `409 {conflict}`; the UI asks:
**a new version of it** (`X-Project`) or **a separate BRD** (`X-As-New`). An identical file
is a no-op. A new version is processed into a **revision**:

* `align()` pairs new requirements with live ones — identical content → same requirement id →
  text similarity (Arabic-normalized word Jaccard ≥ 0.45). Each live row is used once.
* Items: **same / modified / added / removed**, each **removed / modified** one with its
  impact (use cases citing it, tested ones).
* The user unticks what they don't want: keep a removed requirement, keep the current text of
  a modified one, leave out an addition. **Apply** snapshots the BRD first (undo in Versions),
  then `reconcile()`s: same → reordered in place, modified → **updated in place (same id)**,
  added → inserted, removed → **archived**. `revision_no` +1, caches busted.
* **Full-replacement guard:** applies once the live BRD has **≥5 requirements**; it triggers
  when fewer than **20%** of them would still match the user's choices — `same` + `modified`
  count as matched, and so does a `removed` requirement the user unticks (keeps). That pattern
  almost always means the wrong document or unreadable text, so the review shows a warning and
  **Apply** asks for an explicit "Replace everything" confirmation. Enforced by the API too, not
  just the UI: an unconfirmed apply under that threshold is refused with
  `409 {confirm_required: true, summary}`.
* If the live BRD was edited after the comparison, apply refuses (`Stale`) and the UI
  **re-compares** (no re-extraction, no re-embedding).
* `POST /ingest/reprocess` re-runs the pipeline on the stored source as a revision (e.g. after a
  parser improvement).

## 3. Deletes — the rules

| What | How it is deleted | What survives |
|---|---|---|
| A requirement removed by a new version, the requirement editor, a snapshot restore or a draft discard | **Archived**: `archive.retire()` moves the row (same **id**, its vector) to `brd_chunk_archive`. **Nothing hard-deletes a requirement** — true of the UI/API path, and now also of the CLI: `backend/ingest/pipeline.py` for a `.pdf`/`.docx` goes through the same `jobs.submit()`/job system as the upload flow, so it never bypasses this. Its Markdown-only legacy path (for the synthetic demo DB) predates archiving and refuses to shrink a document instead of deleting rows — and now refuses **outright** against a real (post-ingestion-v2) database (detected by `brd_chunk_archive`'s presence), since it can neither archive nor honor `brd_real`'s DEFERRABLE unique constraint — see the pipeline module's docstring. | Every reference resolves: `use_case.source_chunk_ids`, chat `cited_chunk_ids` (shown with a **removed** tag), `requirement_change.chunk_id` (its FK was dropped so it is never nulled), change proposals. Live search / requirement lists / use-case generation stop seeing it; HNSW is not polluted. A restore **revives it under the same id** with its vector. |
| A snapshot restore / discard | `reconcile()` — ids matched first, archived rows revived, the rest archived | ids of every requirement present in both states |
| A whole BRD (`/delete_brd`) | **One transaction**: the document row, its conversations, its use cases (+ test results + audit), and its ingest jobs/sources/revisions are all removed together. **After** that commit: stored source objects no other source row still references are deleted immediately, and the owner's cached vectors for that BRD's hashes are purged once no remaining requirement / archived one / open revision anywhere still uses them. | nothing — this is the only permanent delete; storage failures are logged. A periodic sweep (`jobs.start_source_gc`, started at API startup, every 6 h) catches any leftover orphaned source objects **and** any cached embedding vector that's been unreferenced **and** idle for 6 h (`brd_embedding_cache.last_used_at`) — not a retry of a failed delete, just a backstop for the rare crash mid-cleanup; test-evidence objects are swept separately (`use_cases.testing`, via `storage.gc_orphans`). |
| A discarded new version | Marked `discarded`; its items stay as the record of what was uploaded; its source object stays with the BRD | the live BRD, untouched |

## 4. Voyage free tier (3 req/min, 10K tokens/min)

`backend/core/voyage.py` keeps **one** sliding-window budget for requests **and** tokens —
**per server process**, not shared across processes, so it only enforces the free-tier cap
correctly when **one** API process is running. Run a single uvicorn worker on the free tier;
a multi-process/multi-worker deployment needs a paid Voyage tier or a shared external limiter.
Interactive calls (`priority="query"`: chat embeddings, rerank, a
requirement edit) always go first; background ingestion (`"ingest"`) never overtakes a
waiting query and, while chat was active in the last 90 s, leaves `VOYAGE_QUERY_RESERVE_RPM`
slots free. Ingest batches are sized to ~3.5K estimated tokens and the chars→tokens ratio is
learned from real usage (Arabic ≈ more tokens per char). A 429 pauses every caller; transient
errors retry, then `VoyageUnavailable`. Throughput ceiling ≈ 9K tokens/min ⇒ ~20–25
requirements/min for a new BRD; re-uploads only embed what changed.

## 5. Object storage

`STORAGE_BACKEND=s3` (MinIO locally — `docker compose up -d storage`, console on :9001;
AWS S3 / R2 by changing `S3_*`) or `local` (dev/tests). Keys are content-addressed per owner:
`brd-sources/<owner>/<sha256><ext>`. `GET /brd/source/{id}` downloads the original (owner-only,
always as an attachment).

## 6. Configuration (.env)

`STORAGE_BACKEND`, `STORAGE_BUCKET`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`,
`S3_REGION` · `VOYAGE_MAX_RPM` (3), `VOYAGE_MAX_TPM` (10000), `VOYAGE_QUERY_RESERVE_RPM` (1) ·
`INGEST_MAX_BYTES` (25 MB), `INGEST_MAX_PAGES` (600), `INGEST_MAX_CHUNKS` (2500),
`INGEST_MAX_ATTEMPTS` (4), `INGEST_UPLOADS_PER_HOUR` (20).

## 7. Endpoints

`POST /upload` (headers `X-Filename`, `X-Title`, `X-Project`, `X-As-New`) · `GET /ingest/jobs` ·
`POST /ingest/jobs/retry|cancel` · `POST /ingest/reprocess` · `GET /brd/source/{id}` ·
`GET /brd/revision?project=` · `POST /brd/revision/apply|discard|recompare`.

## 8. Known limits / next

* **OCR** is not configured — scanned pages are reported, not read.
* BRDs ingested **before v2** have no stored source and structure-blind chunks; re-upload the
  file as a new version to move them to structure-aware requirements (the comparison will show
  most requirements as modified/removed/added because the chunk boundaries change).
* `backend/legacy/brd_pipeline/` (the old structure-blind upload path) is no longer used by `/upload`
  or by the CLI; its pure modules remain for their tests.
* `backend/ingest/pipeline.py` (the `python -m backend.ingest.pipeline` CLI) now routes a
  `.pdf`/`.docx` through `jobs.submit()` — the same path as the UI upload. Its Markdown-only
  path is legacy, kept just for seeding the synthetic `brd` demo DB used by the golden-set eval.
* How requirement changes propagate to use cases / test results is the next design step
  ("change requirement handling").

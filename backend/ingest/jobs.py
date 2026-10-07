"""Durable BRD ingestion jobs (ingestion v2). See docs/INGESTION.md.

  submit()        validate (content sniff, size, per-user rate, title conflict, one active job
                  per BRD) -> store the source in object storage -> queue an ingest_job.
                  mode 'new'      : a new BRD (live as soon as it is processed)
                  mode 'revision' : a new version of an existing BRD — processed, COMPARED and
                                    staged for review; the live BRD keeps answering meanwhile
  worker          one background thread per process claims jobs with FOR UPDATE SKIP LOCKED
                  (safe with any number of processes); a lease reclaims a crashed worker's job.
                  Every job write after claim is fenced to (status='running' AND attempts==the
                  claimed value) — `_claim()`'s `attempts` doubles as a cheap claim token, so a
                  worker that lost its lease (reclaimed, or canceled) writes nothing more.
  _run()          extract -> chunk (structure-aware) -> embed (cache-aware, checkpointed,
                  ingest priority) -> store (new, idempotent re-run safe) | compare (revision)
  errors          a bad document fails at once with its reason; outages (Voyage, storage, DB)
                  retry with backoff up to INGEST_MAX_ATTEMPTS, resuming from the embedding cache

Deletes: `purge_after_brd_delete` removes the BRD's stored source objects (only when no other
source row still references the same content-addressed object) and — among the hashes
`brd_embed_hashes` collected before the delete, past a grace period — the owner's cached
vectors no remaining requirement / archived requirement / open revision uses.
`start_source_gc` periodically sweeps object-storage leaks (a put whose DB insert then failed).
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from backend.core import cache, storage
from backend.core.config import settings
from backend.core.db import pool
from backend.ingest import chunker, extract, parser, revise
from backend.core.voyage import VoyageUnavailable, client, guarded

log = logging.getLogger("brd.ingest")

LEASE_SECONDS = 15 * 60
_BACKOFF = (60, 300, 900, 1800)          # seconds between retries of a transient failure
_BATCH_TOKENS = 3500                     # per request: 2-3 requests/min stay under the 10K TPM cap
_BATCH_TEXTS = 64
_MIMES = {"pdf": "application/pdf",
          "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


class UploadRejected(ValueError):
    def __init__(self, status: int, message: str, payload: dict | None = None):
        super().__init__(message)
        self.status, self.payload = status, payload or {}


class Canceled(RuntimeError):
    pass


# --- naming ---------------------------------------------------------------------------

def slugify(title: str) -> str:
    """Unicode-safe BRD id: keeps letters of every script (Arabic titles no longer collapse
    to 'brd'), lower-cases, joins words with '-'."""
    t = unicodedata.normalize("NFKC", title or "").lower()
    slug = re.sub(r"[^\w]+", "-", t, flags=re.UNICODE).strip("-_")
    return slug[:80].strip("-") or "brd"


def derive_title(filename: str, override: str | None = None) -> str:
    if override and override.strip():
        return " ".join(override.split())[:200]
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", filename or "").strip()
    pretty = " ".join(re.sub(r"[_\-]+", " ", stem).split())
    return (pretty[:1].upper() + pretty[1:]) if pretty else "Untitled BRD"


def _unique_project(cur, owner_id: int, base: str) -> str:
    cur.execute("select project from brd_document where owner_id = %s and (project = %s or project like %s)",
                (owner_id, base, f"{base}-%"))
    taken = {r[0] for r in cur.fetchall()}
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


# --- submit -----------------------------------------------------------------------------

def submit(owner_id: int, by: int, data: bytes, filename: str, *, title: str | None = None,
           target_project: str | None = None, as_new: bool = False) -> dict:
    if not data:
        raise UploadRejected(400, "The file is empty")
    if len(data) > settings.ingest_max_bytes:
        raise UploadRejected(413, f"File too large (max {settings.ingest_max_bytes // (1024 * 1024)} MB)")
    try:
        kind = extract.sniff(data, filename)
    except extract.ExtractError as e:
        raise UploadRejected(400, str(e)) from e
    sha = hashlib.sha256(data).hexdigest()

    with pool().connection() as conn, conn.cursor() as cur:
        # counts ingest_upload_log rows (NOT ingest_job — delete_brd's purge_project_rows
        # deletes every ingest_job row for the project, which used to reset this window on an
        # upload -> delete -> re-upload cycle; the log is append-only and outlives the BRD).
        # Opportunistically pruned here so the table never grows unbounded.
        cur.execute("delete from ingest_upload_log where created_at < now() - interval '1 day'")
        cur.execute("select count(*) from ingest_upload_log where owner_id = %s and created_at > now() - interval '1 hour'",
                    (owner_id,))
        if cur.fetchone()[0] >= settings.ingest_uploads_per_hour:
            raise UploadRejected(429, "Upload limit reached — try again in a little while")
        if target_project:
            cur.execute("select d.id, d.title, s.sha256, d.status from brd_document d "
                        "left join brd_source s on s.id = d.source_id "
                        "where d.owner_id = %s and d.project = %s order by d.id desc limit 1",
                        (owner_id, target_project))
            row = cur.fetchone()
            if not row:
                raise UploadRejected(404, "That BRD no longer exists")
            doc_id, doc_title, existing_sha, doc_status = row
            # only short-circuit on a LIVE ('ready') BRD — a 'failed' or still-'processing' one
            # with the same sha needs its job (re)run, not a silent no-op.
            if existing_sha == sha and doc_status == "ready":
                return {"unchanged": True, "project": target_project, "title": doc_title,
                        "message": "This file is identical to the current version — nothing to update."}
            mode, project, resolved_title = "revision", target_project, doc_title
        else:
            resolved_title = derive_title(filename, title)
            base = slugify(resolved_title)
            # a clash is either the project id itself (unrenamed BRD) OR — since rename only
            # ever touches `title`, never `project` — another of this owner's BRDs whose
            # CURRENT title slugifies to the same thing (a renamed BRD re-uploaded under its
            # new name used to slip past this check entirely).
            cur.execute("select project, title from brd_document where owner_id = %s", (owner_id,))
            existing = next((r for r in cur.fetchall() if r[0] == base or slugify(r[1]) == base), None)
            if existing and not as_new:
                raise UploadRejected(409, "A BRD with this name already exists",
                                     {"conflict": {"project": existing[0], "title": existing[1]}})
            project = _unique_project(cur, owner_id, base) if existing else base
            mode, doc_id = "new", None
        cur.execute("select 1 from ingest_job where owner_id = %s and project = %s and status in ('queued', 'running')",
                    (owner_id, project))
        if cur.fetchone():
            raise UploadRejected(409, "This BRD is already being processed — wait for it to finish")

    key = storage.source_key(owner_id, sha, filename)
    try:
        storage.store().put(key, data, _MIMES[kind])      # content-addressed -> put is idempotent;
    except storage.StorageError as e:                      # always put (never "if not exists") so a
        log.exception("upload: object storage failed")    # concurrent BRD-delete purge racing the
        raise UploadRejected(503, "File storage is unavailable right now — please try again") from e  # old exists()
    except RuntimeError as e:                               # check can never win against this upload
        log.exception("upload: object storage not configured")
        raise UploadRejected(503, "File storage isn't configured on the server — contact the "
                                  "administrator.") from e

    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "insert into brd_source (owner_id, project, object_key, filename, mime, size, sha256, uploaded_by) "
                "values (%s,%s,%s,%s,%s,%s,%s,%s) returning id",
                (owner_id, project, key, filename[:255], _MIMES[kind], len(data), sha, by))
            source_id = cur.fetchone()[0]
            if mode == "new":
                cur.execute(
                    "insert into brd_document (title, project, version, status, sha256, owner_id, chunks_done, "
                    "chunks_total, proc_started_at, source_id, revision_no, context_title) "
                    "values (%s, %s, 'v1', 'processing', %s, %s, 0, null, now(), %s, 1, %s) returning id",
                    (resolved_title, project, sha, owner_id, source_id, resolved_title))
                doc_id = cur.fetchone()[0]
            cur.execute("update brd_source set document_id = %s where id = %s", (doc_id, source_id))
            cur.execute(
                "insert into ingest_job (owner_id, project, document_id, source_id, mode, created_by, max_attempts) "
                "values (%s,%s,%s,%s,%s,%s,%s) returning id",
                (owner_id, project, doc_id, source_id, mode, by, settings.ingest_max_attempts))
            job_id = cur.fetchone()[0]
            cur.execute("insert into ingest_upload_log (owner_id) values (%s)", (owner_id,))
            conn.commit()
    except psycopg.errors.UniqueViolation as e:
        raise UploadRejected(409, "This BRD is already being processed — wait for it to finish") from e
    wake()
    return {"ok": True, "job_id": job_id, "project": project, "title": resolved_title, "mode": mode,
            "status": "queued"}


def reprocess(owner_id: int, by: int, project: str) -> dict:
    """Re-run the pipeline on the BRD's stored source (e.g. after a parser improvement) as a
    revision to review — nothing changes until the user applies it."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("delete from ingest_upload_log where created_at < now() - interval '1 day'")
        cur.execute("select count(*) from ingest_upload_log where owner_id = %s and created_at > now() - interval '1 hour'",
                    (owner_id,))
        if cur.fetchone()[0] >= settings.ingest_uploads_per_hour:
            raise UploadRejected(429, "Upload limit reached — try again in a little while")
        cur.execute("select d.id, d.source_id from brd_document d where d.owner_id = %s and d.project = %s "
                    "order by d.id desc limit 1", (owner_id, project))
        row = cur.fetchone()
        if not row:
            raise UploadRejected(404, "That BRD no longer exists")
        if not row[1]:
            raise UploadRejected(409, "This BRD was ingested before source files were stored — upload the file again")
        try:
            cur.execute("insert into ingest_job (owner_id, project, document_id, source_id, mode, created_by, max_attempts) "
                        "values (%s,%s,%s,%s,'revision',%s,%s) returning id",
                        (owner_id, project, row[0], row[1], by, settings.ingest_max_attempts))
        except psycopg.errors.UniqueViolation as e:
            raise UploadRejected(409, "This BRD is already being processed") from e
        job_id = cur.fetchone()[0]
        cur.execute("insert into ingest_upload_log (owner_id) values (%s)", (owner_id,))
        conn.commit()
    wake()
    return {"ok": True, "job_id": job_id, "project": project, "mode": "revision", "status": "queued"}


# --- worker -----------------------------------------------------------------------------

_event = threading.Event()
_started = False
_start_lock = threading.Lock()


def wake() -> None:
    _event.set()


def start_worker() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="ingest-worker", daemon=True).start()
    log.info("ingest worker started")


def _loop() -> None:
    while True:
        try:
            job = _claim()
            if job:
                _run(job)
                continue
        except Exception:  # noqa: BLE001 — the worker must never die
            log.exception("ingest worker loop error")
        _event.wait(10)
        _event.clear()


def _claim() -> dict | None:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """update ingest_job set status = 'running', attempts = attempts + 1, locked_at = now(),
                      started_at = coalesce(started_at, now()), updated_at = now(), error = null
               where id = (select id from ingest_job
                           where (status = 'queued' and next_attempt_at <= now())
                              or (status = 'running' and locked_at < now() - make_interval(secs => %s))
                           order by created_at for update skip locked limit 1)
               returning id, owner_id, project, document_id, source_id, mode, attempts, max_attempts""",
            (LEASE_SECONDS,))
        r = cur.fetchone()
        conn.commit()
    if not r:
        return None
    return dict(zip(("id", "owner_id", "project", "document_id", "source_id", "mode", "attempts", "max_attempts"), r))


def _update(job: dict, **cols) -> bool:
    """Write a job row, FENCED to the claim this worker holds: status='running' AND the
    `attempts` value _claim() handed back (bumped on every claim/reclaim — a cheap,
    column-free fencing token). If another worker reclaimed the lease, or the job was
    canceled, this is a no-op (0 rows) instead of clobbering it. Returns whether it wrote."""
    sets = ", ".join(f"{k} = %s" for k in cols) + ", updated_at = now(), locked_at = now()"
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(f"update ingest_job set {sets} where id = %s and status = 'running' and attempts = %s",
                    (*cols.values(), job["id"], job["attempts"]))
        ok = cur.rowcount > 0
        conn.commit()
    return ok


def _check_live(job: dict) -> None:
    """Heartbeat + cancel/lease-loss check between batches. Fenced the same way as _update:
    a stale `attempts` (another worker reclaimed the lease) or a non-'running' status (user
    canceled) both read as "not alive"."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update ingest_job set locked_at = now() where id = %s and status = 'running' "
                    "and attempts = %s returning id", (job["id"], job["attempts"]))
        alive = cur.fetchone()
        conn.commit()
    if not alive:
        raise Canceled()


def _job_fence(job: dict):
    """A callable(cur) that re-checks, UNDER `for update` IN THE CALLER'S transaction, that
    this job is still 'running' with the SAME claim token (`attempts`) _run started with —
    the same fencing `_update`/`_check_live` apply to column writes, extended to writes made
    directly inside another transaction (revise.compare's staged revision; _store_new's
    publish). A canceled job, or one whose lease was reclaimed by another worker, fails this
    and raises Canceled — the whole transaction (including anything already written in it)
    rolls back, so nothing partial is ever published or staged."""
    def _check(cur) -> None:
        cur.execute("select 1 from ingest_job where id = %s and status = 'running' and attempts = %s for update",
                    (job["id"], job["attempts"]))
        if not cur.fetchone():
            raise Canceled()
    return _check


def _doc_progress(job: dict, done: int, total: int) -> None:
    if job["mode"] != "new":
        return
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update brd_document set chunks_done = %s, chunks_total = %s where id = %s",
                    (done, total, job["document_id"]))
        conn.commit()


def _run(job: dict) -> None:
    t0 = time.monotonic()
    stats: dict = {}
    if job["attempts"] > job["max_attempts"]:                 # kept dying mid-run (crash / OOM)
        _fail(job, "Processing kept getting interrupted — gave up after repeated attempts", stats, retry=False)
        return
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select object_key, filename from brd_source where id = %s", (job["source_id"],))
            src = cur.fetchone()
            # the STABLE embed-context title (never touched by rename — see migration 036),
            # not the live display title, so renaming a BRD never forces a re-embed.
            cur.execute("select coalesce(context_title, title) from brd_document where id = %s", (job["document_id"],))
            doc = cur.fetchone()
        if not src or not doc:
            raise Canceled()                                   # BRD deleted meanwhile
        key, filename = src
        title = doc[0]

        _update(job, stage="extract")
        data = storage.store().get(key)
        ex = extract.extract(data, filename)
        _update(job, stage="chunk", warnings=Jsonb(ex.warnings) if ex.warnings else None)
        chunks = chunker.build_chunks(parser.parse_text(ex.markdown, title))
        if not chunks:
            raise extract.ExtractError("No requirements could be found in this document")
        if len(chunks) > settings.ingest_max_chunks:
            raise extract.ExtractError(f"This document yields {len(chunks)} requirements; the limit is "
                                       f"{settings.ingest_max_chunks}")
        model = settings.need(settings.embed_model_docs, "EMBED_MODEL_DOCS")
        inputs = [chunker.embed_input(title, c) for c in chunks]
        for c, inp in zip(chunks, inputs):
            c.embed_hash = chunker.embed_hash(model, inp)
        stats.update(requirements=len(chunks), pages=ex.page_count, headings=ex.headings, file_type=ex.file_type,
                     with_req_id=sum(1 for c in chunks if c.req_id))

        _update(job, stage="embed", progress_done=0, progress_total=len(chunks))
        stats.update(_embed(job, model, chunks, inputs))

        if job["mode"] == "new":
            _update(job, stage="store")
            stats["seconds"] = round(time.monotonic() - t0, 1)
            # _store_new marks the job 'done' (with these stats) in the SAME transaction that
            # publishes the BRD, so a cancel can never land between "BRD live" and "job done".
            if _store_new(job, chunks, model, stats):
                log.info("ingest job %s: document %s already stored (idempotent re-run) — done",
                         job["id"], job["document_id"])
            else:
                log.info("ingest job %s done: %s", job["id"], stats)
            return
        else:
            _update(job, stage="compare")
            with pool().connection() as conn, conn.cursor() as cur:
                res = revise.compare(cur, owner_id=job["owner_id"], document_id=job["document_id"],
                                     project=job["project"], job_id=job["id"], source_id=job["source_id"],
                                     new_rows=[{"text": c.text, "content_hash": c.content_hash, "req_id": c.req_id,
                                                "section": c.section, "section_path": c.section_path, "page": c.page,
                                                "token_count": c.token_count, "embed_hash": c.embed_hash}
                                               for c in chunks],
                                     fence=_job_fence(job))
                conn.commit()
            stats["revision_id"], stats["changes"] = res["revision_id"], res["summary"]
            status = "review"
        stats["seconds"] = round(time.monotonic() - t0, 1)
        ok = _update(job, status=status, stage=None, stats=Jsonb(stats),
                    finished_at=None if status == "review" else _now())
        if ok:
            log.info("ingest job %s %s: %s", job["id"], status, stats)
        else:
            log.info("ingest job %s finished (%s) but lost its claim before that could be recorded "
                     "(canceled or reclaimed) — discarding", job["id"], status)
    except Canceled:
        log.info("ingest job %s canceled", job["id"])
        # only the user's cancel() (status actually 'canceled') should fail the doc — a
        # worker that merely LOST its lease (reclaimed by another worker, which is still
        # processing) must not mark the doc failed under it.
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select 1 from ingest_job where id = %s and status = 'canceled'", (job["id"],))
            actually_canceled = cur.fetchone() is not None
        if actually_canceled:
            _mark_doc_failed(job, "Processing was canceled")
    except extract.ExtractError as e:                          # the document itself is the problem
        _fail(job, str(e), stats, retry=False)
    except (VoyageUnavailable, storage.StorageError, psycopg.OperationalError) as e:
        _fail(job, f"{type(e).__name__}: {e}", stats, retry=True)
    except Exception as e:  # noqa: BLE001 — unknown: retry a few times, then fail with the reason
        log.exception("ingest job %s crashed", job["id"])
        _fail(job, f"Unexpected error ({type(e).__name__}): {e}", stats, retry=True)


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _fail(job: dict, message: str, stats: dict, *, retry: bool) -> None:
    """Both branches are fenced (status='running' AND attempts=job's claim token) so this
    never clobbers a job another worker has since reclaimed, or — the original bug — a job
    the user canceled in the meantime (previously the terminal branch's UPDATE had no status
    filter at all, so a late ExtractError after a cancel silently turned 'canceled' back into
    'failed'). brd_document is only touched when the job write actually took effect."""
    if retry and job["attempts"] < job["max_attempts"]:
        wait = _BACKOFF[min(job["attempts"] - 1, len(_BACKOFF) - 1)]
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("update ingest_job set status = 'queued', error = %s, updated_at = now(), locked_at = null, "
                        "next_attempt_at = now() + make_interval(secs => %s) where id = %s and status = 'running' "
                        "and attempts = %s", (message[:2000], wait, job["id"], job["attempts"]))
            ok = cur.rowcount > 0
            conn.commit()
        if ok:
            log.warning("ingest job %s will retry in %ss: %s", job["id"], wait, message)
        else:
            log.info("ingest job %s lost its claim before the retry could be recorded — leaving it", job["id"])
        return
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update ingest_job set status = 'failed', error = %s, stats = %s, finished_at = now(), "
                    "updated_at = now() where id = %s and status = 'running' and attempts = %s",
                    (message[:2000], Jsonb(stats), job["id"], job["attempts"]))
        ok = cur.rowcount > 0
        conn.commit()
    if ok:
        _mark_doc_failed(job, message)
        log.warning("ingest job %s failed: %s", job["id"], message)
    else:
        log.info("ingest job %s lost its claim before it could be marked failed — leaving it "
                 "(canceled or reclaimed)", job["id"])


def _mark_doc_failed(job: dict, message: str) -> None:
    if job["mode"] != "new":                                   # a failed revision leaves the live BRD as is
        return
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update brd_document set status = 'failed', last_error = %s where id = %s and status = 'processing'",
                    (message[:2000], job["document_id"]))
        conn.commit()


def _embed(job: dict, model: str, chunks, inputs: list[str]) -> dict:
    """Embed only what the owner's cache doesn't have; every batch is committed to the cache
    immediately, so a crash / retry resumes where it stopped and a re-upload re-embeds only
    changed requirements."""
    owner = job["owner_id"]
    hashes = [c.embed_hash for c in chunks]
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select input_hash from brd_embedding_cache where owner_id = %s and model = %s "
                    "and input_hash = any(%s)", (owner, model, list(set(hashes))))
        have = {r[0] for r in cur.fetchall()}
        if have:
            # bump last_used_at on every hit — the grace period AND the periodic vector
            # sweep both key off this, not created_at, so a cache row this job just reused
            # can't be purged out from under it before _store_new copies it.
            cur.execute("update brd_embedding_cache set last_used_at = now() where owner_id = %s and model = %s "
                        "and input_hash = any(%s)", (owner, model, list(have)))
            conn.commit()
    todo: dict[str, str] = {}
    for h, inp in zip(hashes, inputs):
        if h not in have:
            todo.setdefault(h, inp)
    total = len(chunks)
    done = sum(1 for h in hashes if h in have)
    _update(job, progress_done=done, progress_total=total)
    _doc_progress(job, done, total)
    ratio, tokens, embedded = 1.0, 0, 0
    items = list(todo.items())
    i = 0
    while i < len(items):
        batch, est = [], 0
        while i < len(items) and len(batch) < _BATCH_TEXTS:
            e = max(1, int(len(items[i][1]) / 4 * ratio))
            if batch and est + e > _BATCH_TOKENS:
                break
            batch.append(items[i])
            est += e
            i += 1
        _check_live(job)
        res = guarded(client().embed, [t for _, t in batch], model=model, input_type="document",
                      priority="ingest", est_tokens=est)
        vecs = res.embeddings
        if vecs and len(vecs[0]) != 1024:
            raise extract.ExtractError(f"Embedding model returned dimension {len(vecs[0])}, expected 1024")
        if res.total_tokens and est:
            ratio = max(1.0, ratio * res.total_tokens / est)    # learn the real chars->tokens ratio
        with pool().connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "insert into brd_embedding_cache (owner_id, model, input_hash, embedding, last_used_at) "
                "values (%s,%s,%s,%s::vector,now()) "
                "on conflict (owner_id, model, input_hash) do update set last_used_at = now()",
                [(owner, model, h, "[" + ",".join(f"{x:.8f}" for x in v) + "]") for (h, _), v in zip(batch, vecs)])
            conn.commit()
        tokens += res.total_tokens or 0
        embedded += len(batch)
        batch_hashes = {h for h, _ in batch}
        done += sum(1 for h in hashes if h in batch_hashes)
        _update(job, progress_done=min(done, total))
        _doc_progress(job, min(done, total), total)
    return {"embedded": embedded, "reused": total - embedded, "tokens": tokens}


def _store_new(job: dict, chunks, model: str, stats: dict | None = None) -> bool:
    """Publish a new BRD in ONE transaction; vectors are copied server-side from the cache.

    Idempotent: if the document already has chunks (status='ready', or rows already present —
    an earlier run of THIS job already committed the store, then crashed / lost its lease
    before the job could be marked done), nothing is deleted or re-inserted — re-running the
    delete+insert here would hard-delete the now-live BRD's requirements and re-create them
    under NEW ids, breaking every citation. Instead the job is marked 'done' in this SAME
    transaction (fenced to this worker's claim) and True is returned so the caller skips the
    generic status update. Returns False on a normal (first) store — which ALSO marks the job
    done (with `stats`) inside its transaction, so "BRD published" and "job done" commit together."""
    cols = list(zip(*[(c.req_id, c.section, c.section_path, c.page, c.ordinal, c.text, c.token_count,
                       c.content_hash, c.embed_hash) for c in chunks]))
    with pool().connection() as conn, conn.cursor() as cur:
        _job_fence(job)(cur)                            # before ANY write — a cancel mid-store must publish nothing
        cur.execute("select status from brd_document where id = %s for update", (job["document_id"],))
        row = cur.fetchone()
        if not row:
            raise Canceled()
        cur.execute("select exists(select 1 from brd_chunk where document_id = %s)", (job["document_id"],))
        already_stored = row[0] == "ready" or cur.fetchone()[0]
        if already_stored:
            cur.execute("update ingest_job set status = 'done', updated_at = now(), locked_at = now() "
                        "where id = %s and status = 'running' and attempts = %s",
                        (job["id"], job["attempts"]))
            conn.commit()
            return True
        cur.execute("delete from brd_chunk where document_id = %s", (job["document_id"],))   # first store: nothing referenced yet
        cur.execute(
            """insert into brd_chunk (document_id, req_id, section, section_path, page, ordinal, text, token_count,
                                      content_hash, embed_hash)
               select %s, * from unnest(%s::text[], %s::text[], %s::text[], %s::int[], %s::int[], %s::text[],
                                        %s::int[], %s::text[], %s::text[])""",
            (job["document_id"], *[list(c) for c in cols]))
        cur.execute(
            """insert into brd_embedding (chunk_id, model, embedding)
               select c.id, %s, ec.embedding from brd_chunk c
               join brd_embedding_cache ec on ec.owner_id = %s and ec.model = %s and ec.input_hash = c.embed_hash
               where c.document_id = %s""",
            (model, job["owner_id"], model, job["document_id"]))
        if cur.rowcount != len(chunks):
            raise RuntimeError(f"embedding copy mismatch ({cur.rowcount} of {len(chunks)})")
        cur.execute("update brd_document set status = 'ready', chunks_done = %s, chunks_total = %s, last_error = null, "
                    "updated_at = now() where id = %s", (len(chunks), len(chunks), job["document_id"]))
        cur.execute("update ingest_job set status = 'done', stage = null, stats = %s, finished_at = now(), "
                    "updated_at = now(), locked_at = now() where id = %s and status = 'running' and attempts = %s",
                    (Jsonb(stats or {}), job["id"], job["attempts"]))   # the fence above holds this row's lock
        conn.commit()
    cache.bust_project(job["project"], owner_id=job["owner_id"])
    try:
        from backend.chat.suggest import invalidate
        invalidate(job["project"], job["owner_id"])
    except Exception:  # noqa: BLE001
        pass
    return False


# --- job controls / reads ---------------------------------------------------------------

def retry(owner_id: int, job_id: int) -> bool:
    """Re-queue a failed job. `attempts` is NEVER reset — it's also the fencing token
    _update/_check_live/_job_fence compare writes against, so resetting it to 0 would let a
    stalled old worker's writes (still holding the pre-retry `attempts` value) pass the fence
    again on the NEXT retry (ABA). Instead `max_attempts` is raised to `attempts + a fresh
    budget`, so the job gets INGEST_MAX_ATTEMPTS more tries with a token that only ever goes up.
    Can collide with ingest_job_active_uk (another job already active for the same BRD) if a
    revision/reprocess job got queued in the meantime — caught and turned into a clean False
    (the API already maps that to a refusal) instead of a 500."""
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("update ingest_job set status = 'queued', max_attempts = attempts + %s, "
                        "next_attempt_at = now(), error = null, finished_at = null, updated_at = now() "
                        "where id = %s and owner_id = %s and status = 'failed' "
                        "returning mode, document_id", (settings.ingest_max_attempts, job_id, owner_id))
            r = cur.fetchone()
            if r and r[0] == "new":
                cur.execute("update brd_document set status = 'processing', last_error = null, proc_started_at = now() "
                            "where id = %s", (r[1],))
            conn.commit()
    except psycopg.errors.UniqueViolation:
        return False
    if r:
        wake()
    return bool(r)


def cancel(owner_id: int, job_id: int) -> bool:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update ingest_job set status = 'canceled', finished_at = now(), updated_at = now() "
                    "where id = %s and owner_id = %s and status in ('queued', 'running') returning mode, document_id",
                    (job_id, owner_id))
        r = cur.fetchone()
        if r and r[0] == "new":
            cur.execute("update brd_document set status = 'failed', last_error = 'Processing was canceled' "
                        "where id = %s and status = 'processing'", (r[1],))
        conn.commit()
    return bool(r)


def jobs(owner_id: int, project: str | None = None, limit: int = 20) -> list[dict]:
    where, params = "where j.owner_id = %s", [owner_id]
    if project:
        where += " and j.project = %s"
        params.append(project)
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"select j.id, j.project, j.mode, j.status, j.stage, j.progress_done, j.progress_total, j.attempts, "
            f"j.max_attempts, j.error, j.warnings, j.stats, j.created_at, j.finished_at, j.next_attempt_at, s.filename "
            f"from ingest_job j left join brd_source s on s.id = j.source_id {where} "
            f"order by j.created_at desc limit %s", (*params, limit))
        rows = cur.fetchall()
    return [{"id": r[0], "project": r[1], "mode": r[2], "status": r[3], "stage": r[4], "done": r[5], "total": r[6],
             "attempts": r[7], "max_attempts": r[8], "error": r[9], "warnings": r[10] or [], "stats": r[11] or {},
             "created_at": r[12].isoformat(), "finished_at": r[13].isoformat() if r[13] else None,
             "next_attempt_at": r[14].isoformat() if r[14] else None, "filename": r[15]} for r in rows]


def source_blob(owner_id: int, source_id: int) -> tuple[bytes, str, str] | None:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select object_key, mime, filename from brd_source where id = %s and owner_id = %s",
                    (source_id, owner_id))
        r = cur.fetchone()
    if not r:
        return None
    return storage.store().get(r[0]), r[1] or "application/octet-stream", r[2]


# --- deletes ----------------------------------------------------------------------------

def source_keys(cur, owner_id: int, project: str) -> list[str]:
    """Object keys of a BRD's sources (collected BEFORE the BRD rows are deleted)."""
    cur.execute("select distinct object_key from brd_source where owner_id = %s and project = %s", (owner_id, project))
    return [r[0] for r in cur.fetchall()]


def purge_project_rows(cur, owner_id: int, project: str) -> None:
    """Rows not reached by the brd_document cascade (e.g. a source whose job failed before
    its document row existed) — in the caller's tx."""
    cur.execute("delete from ingest_job where owner_id = %s and project = %s", (owner_id, project))
    cur.execute("delete from brd_source where owner_id = %s and project = %s", (owner_id, project))
    cur.execute("delete from brd_revision where owner_id = %s and project = %s", (owner_id, project))


_PURGE_GRACE_SECONDS = 30 * 60     # a cache row younger than this is left alone even if it
                                    # LOOKS unreferenced — an in-flight job elsewhere may have
                                    # just embedded it and not committed the referencing chunk yet


def brd_embed_hashes(cur, owner_id: int, project: str) -> list[str]:
    """embed_hash values of this project's documents' chunks, archived chunks and open-revision
    items — collected BEFORE the BRD rows are deleted (like source_keys), so the caller can
    pass them to purge_after_brd_delete and purge ONLY vectors this BRD could have owned."""
    cur.execute(
        """select c.embed_hash from brd_chunk c join brd_document d on d.id = c.document_id
           where d.owner_id = %s and d.project = %s and c.embed_hash is not null
           union
           select a.embed_hash from brd_chunk_archive a join brd_document d on d.id = a.document_id
           where d.owner_id = %s and d.project = %s and a.embed_hash is not null
           union
           select i.embed_hash from brd_revision_item i join brd_revision r on r.id = i.revision_id
           where r.owner_id = %s and r.project = %s and r.status = 'review' and i.embed_hash is not null""",
        (owner_id, project, owner_id, project, owner_id, project))
    return [r[0] for r in cur.fetchall()]


_DELETE_MIN_AGE_SECONDS = 10 * 60  # an identical re-upload's put() re-writes the same
                                    # content-addressed key, refreshing its last-modified time;
                                    # skip deleting anything that fresh (the periodic source-gc
                                    # sweep, which re-lists from scratch, picks up anything
                                    # genuinely orphaned that this skips)


def purge_after_brd_delete(owner_id: int, keys: list[str], hashes: list[str] | None = None) -> dict:
    """After the BRD delete COMMITTED: drop source objects nothing references any more, and —
    only among `hashes` (this deleted BRD's own embed hashes, from brd_embed_hashes BEFORE the
    delete) — the owner's cached vectors still unreferenced by any remaining requirement /
    archived one / open revision, AND not used (hit or (re-)written) in the last grace period.
    The grace period + the narrowed scope matter: purging the owner's WHOLE cache (the old
    behavior) could delete a vector another in-flight ingest job had just embedded (or reused
    from the cache) but not yet committed the chunk that references it, forcing a wasted
    re-embed on a free-tier Voyage budget. `hashes=None` purges NO vectors (backwards-safe for
    any caller not yet passing them).

    Object deletion re-checks brd_source (another upload may have started referencing the same
    content-addressed key between the hashes/keys snapshot and here) AND skips anything the
    store says was modified within `_DELETE_MIN_AGE_SECONDS` — an identical re-upload racing
    this delete would otherwise have its freshly-`put` object removed out from under it."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select distinct object_key from brd_source where object_key = any(%s)", (keys or [""],))
        still = {r[0] for r in cur.fetchall()}
        vectors = 0
        if hashes:
            cur.execute(
                """delete from brd_embedding_cache ec where ec.owner_id = %s and ec.input_hash = any(%s)
                   and ec.last_used_at < now() - make_interval(secs => %s)
                   and not exists (select 1 from brd_chunk c join brd_document d on d.id = c.document_id
                                   where d.owner_id = ec.owner_id and c.embed_hash = ec.input_hash)
                   and not exists (select 1 from brd_chunk_archive a join brd_document d on d.id = a.document_id
                                   where d.owner_id = ec.owner_id and a.embed_hash = ec.input_hash)
                   and not exists (select 1 from brd_revision_item i join brd_revision r on r.id = i.revision_id
                                   where r.owner_id = ec.owner_id and r.status = 'review'
                                         and i.embed_hash = ec.input_hash)""",
                (owner_id, list(set(hashes)), _PURGE_GRACE_SECONDS))
            vectors = cur.rowcount
        conn.commit()
    candidates = [k for k in keys if k not in still]
    objects = 0
    if candidates:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=_DELETE_MIN_AGE_SECONDS)
        try:
            modified = dict(storage.store().list_objects(f"brd-sources/{int(owner_id)}/"))
        except storage.StorageError:
            modified = {}                               # can't verify age — fall back to deleting (old behavior)
        safe = [k for k in candidates if modified.get(k, cutoff) <= cutoff]
        objects = storage.delete_quietly(safe)
    return {"objects_deleted": objects, "vectors_purged": vectors}


# --- orphaned source objects (periodic sweep) -------------------------------------------

_SOURCE_GC_PREFIX = "brd-sources/"
_SOURCE_GC_MIN_AGE = 60 * 60            # 1 hour — lets submit's put-then-insert finish first
_SOURCE_GC_INTERVAL = 6 * 60 * 60       # 6 hours

_source_gc_started = False
_source_gc_lock = threading.Lock()


def sweep_orphan_sources() -> int:
    """One sweep: delete objects under brd-sources/ that no brd_source row references and
    that are older than _SOURCE_GC_MIN_AGE. Catches the two ways a source object can leak: a
    submit() whose object PUT succeeded but the following brd_source insert failed, and a
    delete_quietly() that failed to remove an object after a committed BRD delete. Exposed on
    its own (not just inside the loop) so it can be driven directly, e.g. by tests."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select distinct object_key from brd_source")
        referenced = {r[0] for r in cur.fetchall()}
    try:
        n = storage.gc_orphans(referenced, prefix=_SOURCE_GC_PREFIX, min_age_seconds=_SOURCE_GC_MIN_AGE)
    except storage.StorageError:
        log.warning("source gc: storage unavailable, skipping this sweep", exc_info=True)
        return 0
    if n:
        log.info("source gc: removed %d orphaned source object(s)", n)
    return n


_VECTOR_GC_MIN_AGE = 6 * 60 * 60        # last_used_at, not created_at — a cache row an
                                        # in-flight job just reused must not look stale


def sweep_orphan_vectors() -> int:
    """One sweep, ALL owners: delete brd_embedding_cache rows referenced by no live chunk,
    archived chunk, or open-revision item, AND not used (hit or written) in the last
    _VECTOR_GC_MIN_AGE. Catches vectors `purge_after_brd_delete` never reaches because it only
    runs at BRD-delete time, scoped to that one project's own hashes: a failed/canceled 'new'
    job's embedded-but-never-stored vectors, and an applied/discarded revision's. Exposed on
    its own so it can be driven directly, e.g. by tests."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """delete from brd_embedding_cache ec where ec.last_used_at < now() - make_interval(secs => %s)
               and not exists (select 1 from brd_chunk c join brd_document d on d.id = c.document_id
                               where d.owner_id = ec.owner_id and c.embed_hash = ec.input_hash)
               and not exists (select 1 from brd_chunk_archive a join brd_document d on d.id = a.document_id
                               where d.owner_id = ec.owner_id and a.embed_hash = ec.input_hash)
               and not exists (select 1 from brd_revision_item i join brd_revision r on r.id = i.revision_id
                               where r.owner_id = ec.owner_id and r.status = 'review'
                                     and i.embed_hash = ec.input_hash)""",
            (_VECTOR_GC_MIN_AGE,))
        n = cur.rowcount
        conn.commit()
    if n:
        log.info("vector gc: removed %d orphaned cached vector(s)", n)
    return n


def start_source_gc() -> None:
    """Daemon thread: sweeps orphaned source objects AND orphaned cached vectors at startup,
    then every 6 hours."""
    global _source_gc_started
    with _source_gc_lock:
        if _source_gc_started:
            return
        _source_gc_started = True

    def _loop() -> None:
        while True:
            try:
                sweep_orphan_sources()
            except Exception:  # noqa: BLE001 — the sweeper must never die
                log.exception("source gc sweep error")
            try:
                sweep_orphan_vectors()
            except Exception:  # noqa: BLE001
                log.exception("vector gc sweep error")
            time.sleep(_SOURCE_GC_INTERVAL)

    threading.Thread(target=_loop, name="source-gc", daemon=True).start()
    log.info("source gc sweeper started")

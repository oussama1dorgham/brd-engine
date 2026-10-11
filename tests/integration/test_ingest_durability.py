"""Durability / safety tests for the ingest worker (backend/ingest/jobs.py) against the LOCAL
stack (brd_real Postgres + the configured object store). NO Voyage calls: embeddings are
fabricated and inserted directly into brd_embedding_cache, never requested from the network.

Collected only with RUN_INTEGRATION=1 (see tests/integration/conftest.py). Each test creates
its own throwaway rows/objects under a unique zz-it- prefix and cleans them up itself; never
touches a real BRD.
"""
import time
import uuid
from datetime import datetime, timezone

import pytest
from psycopg.types.json import Jsonb

from backend.core import storage
from backend.core.config import settings
from backend.core.db import pool
from backend.ingest import jobs
from backend.ingest.models import Chunk

MODEL = "zz-it-test-embed-model"


def _owner() -> int:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select id from app_user order by id limit 1")
        return cur.fetchone()[0]


def _vec() -> str:
    return "[" + ",".join("0.0" for _ in range(1024)) + "]"


# --- 1. _store_new is idempotent on a re-run --------------------------------------------

def test_store_new_idempotent_rerun_keeps_ids_and_deletes_nothing():
    owner = _owner()
    project = f"zz-it-store-{uuid.uuid4().hex[:8]}"
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("insert into brd_document (title, project, sha256, owner_id, status, context_title) "
                    "values (%s,%s,'x',%s,'processing',%s) returning id", (project, project, owner, project))
        doc_id = cur.fetchone()[0]
        cur.execute("insert into ingest_job (owner_id, project, document_id, mode, status, attempts, max_attempts) "
                    "values (%s,%s,%s,'new','running',1,4) returning id", (owner, project, doc_id))
        job_id = cur.fetchone()[0]
        conn.commit()
    chunks = [Chunk(ordinal=0, section="S", req_id="FR-1", text="first", token_count=2,
                     content_hash="zz-h1", embed_hash=f"zz-eh1-{uuid.uuid4().hex[:8]}"),
              Chunk(ordinal=1, section="S", req_id="FR-2", text="second", token_count=2,
                    content_hash="zz-h2", embed_hash=f"zz-eh2-{uuid.uuid4().hex[:8]}")]
    vec = _vec()
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "insert into brd_embedding_cache (owner_id, model, input_hash, embedding) values (%s,%s,%s,%s::vector)",
                [(owner, MODEL, c.embed_hash, vec) for c in chunks])
            conn.commit()
        job = {"id": job_id, "owner_id": owner, "project": project, "document_id": doc_id,
               "source_id": None, "mode": "new", "attempts": 1, "max_attempts": 4}

        first = jobs._store_new(job, chunks, MODEL)
        assert first is False
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select id from brd_chunk where document_id = %s order by ordinal", (doc_id,))
            ids_before = [r[0] for r in cur.fetchall()]
        assert len(ids_before) == 2
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select status from ingest_job where id = %s", (job_id,))
            assert cur.fetchone()[0] == "done"  # published AND marked done in one transaction
            # simulate the legacy crash window (store committed, job still running, then reclaimed)
            cur.execute("update ingest_job set status = 'running' where id = %s", (job_id,))
            conn.commit()

        second = jobs._store_new(job, chunks, MODEL)       # simulate a reclaimed/crashed re-run
        assert second is True
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select id from brd_chunk where document_id = %s order by ordinal", (doc_id,))
            ids_after = [r[0] for r in cur.fetchall()]
            cur.execute("select status from ingest_job where id = %s", (job_id,))
            job_status = cur.fetchone()[0]
        assert ids_after == ids_before          # nothing deleted / re-inserted under new ids
        assert job_status == "done"             # marked done by the idempotent branch itself
    finally:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("delete from brd_document where id = %s", (doc_id,))   # cascades brd_chunk, ingest_job
            cur.execute("delete from brd_embedding_cache where owner_id = %s and input_hash = any(%s)",
                        (owner, [c.embed_hash for c in chunks]))
            conn.commit()


# --- 2. claim-token fencing: a stale write is a no-op; cancel is never overwritten ------

def test_update_stale_token_noop_and_cancel_not_overwritten():
    owner = _owner()
    project = f"zz-it-fence-{uuid.uuid4().hex[:8]}"
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("insert into ingest_job (owner_id, project, mode, status, attempts, max_attempts) "
                    "values (%s,%s,'revision','running',3,4) returning id", (owner, project))
        job_id = cur.fetchone()[0]
        conn.commit()
    try:
        # stale token (row holds attempts=3; this write claims attempts=2) -> no-op
        assert jobs._update({"id": job_id, "attempts": 2}, status="done") is False
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select status from ingest_job where id = %s", (job_id,))
            assert cur.fetchone()[0] == "running"

        # correct token -> writes
        assert jobs._update({"id": job_id, "attempts": 3}, status="running") is True

        # user cancels while this (correctly-tokened) worker is still "mid-run"
        assert jobs.cancel(owner, job_id) is True
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select status from ingest_job where id = %s", (job_id,))
            assert cur.fetchone()[0] == "canceled"

        # the worker's late final write (same token it legitimately held) must NOT clobber it
        assert jobs._update({"id": job_id, "attempts": 3}, status="done") is False
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select status from ingest_job where id = %s", (job_id,))
            assert cur.fetchone()[0] == "canceled"
    finally:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("delete from ingest_job where id = %s", (job_id,))
            conn.commit()


# --- 3. retry() against a colliding active job returns False, not a 500 -----------------

def test_retry_returns_false_on_active_job_conflict():
    owner = _owner()
    project = f"zz-it-retry-{uuid.uuid4().hex[:8]}"
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("insert into ingest_job (owner_id, project, mode, status, attempts, max_attempts) "
                    "values (%s,%s,'revision','failed',4,4) returning id", (owner, project))
        failed_id = cur.fetchone()[0]
        cur.execute("insert into ingest_job (owner_id, project, mode, status, attempts, max_attempts) "
                    "values (%s,%s,'revision','queued',0,4) returning id", (owner, project))
        active_id = cur.fetchone()[0]
        conn.commit()
    try:
        assert jobs.retry(owner, failed_id) is False       # would collide with ingest_job_active_uk
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select status from ingest_job where id = %s", (failed_id,))
            assert cur.fetchone()[0] == "failed"
    finally:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("delete from ingest_job where id = any(%s)", ([failed_id, active_id],))
            conn.commit()


# --- 4. brd_embed_hashes + scoped, grace-period purge ------------------------------------

def test_brd_embed_hashes_and_scoped_purge_respects_grace_and_scope():
    owner = _owner()
    project = f"zz-it-purge-{uuid.uuid4().hex[:8]}"
    h_old = f"zz-h-old-{uuid.uuid4().hex[:8]}"
    h_fresh = f"zz-h-fresh-{uuid.uuid4().hex[:8]}"
    h_unrelated = f"zz-h-unrelated-{uuid.uuid4().hex[:8]}"
    vec = _vec()
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("insert into brd_document (title, project, sha256, owner_id, context_title) "
                    "values (%s,%s,'x',%s,%s) returning id", (project, project, owner, project))
        doc_id = cur.fetchone()[0]
        cur.execute("insert into brd_chunk (document_id, section, ordinal, text, content_hash, embed_hash) "
                    "values (%s,'S',0,'t1','zz-ch1',%s), (%s,'S',1,'t2','zz-ch2',%s)",
                    (doc_id, h_old, doc_id, h_fresh))
        conn.commit()
        hashes = jobs.brd_embed_hashes(cur, owner, project)
    assert set(hashes) == {h_old, h_fresh}

    with pool().connection() as conn, conn.cursor() as cur:
        # the grace check keys off last_used_at (not created_at) — a row an in-flight job
        # just REUSED from the cache must not look stale just because it's old.
        cur.execute("insert into brd_embedding_cache (owner_id, model, input_hash, embedding, created_at, last_used_at) "
                    "values (%s,'m',%s,%s::vector, now() - interval '1 hour', now() - interval '1 hour')",
                    (owner, h_old, vec))
        cur.execute("insert into brd_embedding_cache (owner_id, model, input_hash, embedding, created_at, last_used_at) "
                    "values (%s,'m',%s,%s::vector, now() - interval '1 hour', now())", (owner, h_fresh, vec))
        cur.execute("insert into brd_embedding_cache (owner_id, model, input_hash, embedding, created_at, last_used_at) "
                    "values (%s,'m',%s,%s::vector, now() - interval '1 hour', now() - interval '1 hour')",
                    (owner, h_unrelated, vec))
        conn.commit()
        # simulate the BRD delete having committed: h_old/h_fresh are now unreferenced
        cur.execute("delete from brd_document where id = %s", (doc_id,))
        conn.commit()

    try:
        res = jobs.purge_after_brd_delete(owner, keys=[], hashes=hashes)
        assert res["vectors_purged"] == 1       # only h_old: unreferenced AND past the grace period
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select input_hash from brd_embedding_cache where owner_id = %s and input_hash = any(%s)",
                        (owner, [h_old, h_fresh, h_unrelated]))
            left = {r[0] for r in cur.fetchall()}
        assert left == {h_fresh, h_unrelated}   # in-grace + never-passed-in both survive
    finally:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("delete from brd_embedding_cache where owner_id = %s and input_hash = any(%s)",
                        (owner, [h_old, h_fresh, h_unrelated]))
            conn.commit()


# --- 5. orphan source-object sweep: age + reference aware --------------------------------

def test_sweep_orphan_sources_deletes_old_unreferenced_keeps_referenced_and_fresh(monkeypatch):
    owner = _owner()
    prefix = f"zz-it-source-gc-{uuid.uuid4().hex[:8]}/"
    monkeypatch.setattr(jobs, "_SOURCE_GC_PREFIX", prefix)
    monkeypatch.setattr(jobs, "_SOURCE_GC_MIN_AGE", 1)      # 1s, so the test needn't wait an hour

    st = storage.store()
    old_key, ref_key, fresh_key = prefix + "old.bin", prefix + "referenced.bin", prefix + "fresh.bin"
    st.put(old_key, b"x", "application/octet-stream")
    time.sleep(1.2)

    source_id = None
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "insert into brd_source (owner_id, project, object_key, filename, mime, size, sha256, uploaded_by) "
                "values (%s,%s,%s,'f.bin','application/octet-stream',1,'zz-sha',%s) returning id",
                (owner, "zz-it-source-gc", ref_key, owner))
            source_id = cur.fetchone()[0]
            conn.commit()
        st.put(ref_key, b"x", "application/octet-stream")
        st.put(fresh_key, b"x", "application/octet-stream")

        n = jobs.sweep_orphan_sources()
        assert n >= 1
        assert not st.exists(old_key)      # old + unreferenced -> swept
        assert st.exists(ref_key)          # referenced -> kept regardless of age
        assert st.exists(fresh_key)        # too young -> kept
    finally:
        if source_id is not None:
            with pool().connection() as conn, conn.cursor() as cur:
                cur.execute("delete from brd_source where id = %s", (source_id,))
                conn.commit()
        for k in (old_key, ref_key, fresh_key):
            try:
                st.delete(k)
            except Exception:
                pass


# --- 6. title clash after a rename (project id no longer matches the slug) --------------

def test_title_clash_after_rename_raises_409():
    owner = _owner()
    # settings is a frozen dataclass; bypass __setattr__ to isolate from the real upload-rate
    # window for this owner (restored in `finally`), instead of monkeypatching a frozen field.
    original_limit = jobs.settings.ingest_uploads_per_hour
    object.__setattr__(jobs.settings, "ingest_uploads_per_hour", 10_000)
    project = f"zz-it-rename-{uuid.uuid4().hex[:8]}"
    new_title = f"Zz Renamed Title {uuid.uuid4().hex[:8]}"
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("insert into brd_document (title, project, sha256, owner_id, context_title) "
                    "values (%s,%s,'x',%s,%s) returning id", (project, project, owner, project))
        doc_id = cur.fetchone()[0]
        # rename: ONLY title changes (as backend/chat/history_store.py rename does) — project stays
        cur.execute("update brd_document set title = %s where id = %s", (new_title, doc_id))
        conn.commit()
    try:
        with pytest.raises(jobs.UploadRejected) as exc_info:
            jobs.submit(owner, owner, b"%PDF-1.7 fake pdf body", "whatever.pdf", title=new_title)
        assert exc_info.value.status == 409
        assert exc_info.value.payload["conflict"] == {"project": project, "title": new_title}
    finally:
        object.__setattr__(jobs.settings, "ingest_uploads_per_hour", original_limit)
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("delete from brd_document where id = %s", (doc_id,))
            conn.commit()


# --- 7. cancel mid-store: nothing published, doc stays "processing" ---------------------

def test_store_new_canceled_mid_store_publishes_nothing():
    owner = _owner()
    project = f"zz-it-cancel-store-{uuid.uuid4().hex[:8]}"
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("insert into brd_document (title, project, sha256, owner_id, status, context_title) "
                    "values (%s,%s,'x',%s,'processing',%s) returning id", (project, project, owner, project))
        doc_id = cur.fetchone()[0]
        cur.execute("insert into ingest_job (owner_id, project, document_id, mode, status, attempts, max_attempts) "
                    "values (%s,%s,%s,'new','running',1,4) returning id", (owner, project, doc_id))
        job_id = cur.fetchone()[0]
        # the user cancels while this worker (holding claim token attempts=1) is still
        # mid-run -- the job dict _store_new receives still carries the stale attempts=1.
        cur.execute("update ingest_job set status = 'canceled' where id = %s", (job_id,))
        conn.commit()
    chunks = [Chunk(ordinal=0, section="S", req_id="FR-1", text="first", token_count=2,
                     content_hash="zz-ch1", embed_hash=f"zz-ceh1-{uuid.uuid4().hex[:8]}")]
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("insert into brd_embedding_cache (owner_id, model, input_hash, embedding) "
                        "values (%s,%s,%s,%s::vector)", (owner, MODEL, chunks[0].embed_hash, _vec()))
            conn.commit()
        job = {"id": job_id, "owner_id": owner, "project": project, "document_id": doc_id,
               "source_id": None, "mode": "new", "attempts": 1, "max_attempts": 4}
        with pytest.raises(jobs.Canceled):
            jobs._store_new(job, chunks, MODEL)
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select status from brd_document where id = %s", (doc_id,))
            assert cur.fetchone()[0] == "processing"          # never flipped to "ready"
            cur.execute("select count(*) from brd_chunk where document_id = %s", (doc_id,))
            assert cur.fetchone()[0] == 0                      # nothing published
            cur.execute("select status from ingest_job where id = %s", (job_id,))
            assert cur.fetchone()[0] == "canceled"             # untouched by _store_new
    finally:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("delete from brd_document where id = %s", (doc_id,))
            cur.execute("delete from brd_embedding_cache where owner_id = %s and input_hash = %s",
                        (owner, chunks[0].embed_hash))
            conn.commit()


# --- 8. fenced compare: cancel mid-run raises Canceled before any staging write ----------
#
# revise.compare's "fence" hook hasn't landed yet (contracted, owned by another agent) -- the
# fake below stands in for it, calling fence(cur) exactly where the contract puts it: after
# compare's locks, before its first write. This proves jobs.py's call site passes fence= and
# that _job_fence actually blocks the write when the job was canceled mid-run.

def test_revision_compare_fenced_cancel_stages_nothing(monkeypatch):
    owner = _owner()
    project = f"zz-it-cancel-compare-{uuid.uuid4().hex[:8]}"
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("insert into brd_document (title, project, sha256, owner_id, status, context_title) "
                    "values (%s,%s,'x',%s,'ready',%s) returning id", (project, project, owner, project))
        doc_id = cur.fetchone()[0]
        cur.execute("insert into brd_source (owner_id, project, object_key, filename, mime, size, sha256, uploaded_by) "
                    "values (%s,%s,%s,'f.pdf','application/pdf',1,%s,%s) returning id",
                    (owner, project, f"zz-it-cancel-compare-key-{uuid.uuid4().hex[:8]}",
                     f"zz-sha-{uuid.uuid4().hex[:8]}", owner))
        source_id = cur.fetchone()[0]
        cur.execute("insert into ingest_job (owner_id, project, document_id, source_id, mode, status, "
                    "attempts, max_attempts) values (%s,%s,%s,%s,'revision','running',1,4) returning id",
                    (owner, project, doc_id, source_id))
        job_id = cur.fetchone()[0]
        cur.execute("update ingest_job set status = 'canceled' where id = %s", (job_id,))  # canceled mid-run
        conn.commit()

    chunk = Chunk(ordinal=0, section="S", req_id="FR-1", text="new text", token_count=2, content_hash="zz-rc1")
    model = settings.need(settings.embed_model_docs, "EMBED_MODEL_DOCS")
    inp = jobs.chunker.embed_input(project, chunk)          # project == context_title here
    embed_hash = jobs.chunker.embed_hash(model, inp)

    class _FakeEx:
        markdown, warnings, page_count, headings, file_type = "x", [], 1, 0, "pdf"

    class _FakeStore:
        def get(self, key):
            return b"x"

    monkeypatch.setattr(jobs.storage, "store", lambda: _FakeStore())
    monkeypatch.setattr(jobs.extract, "extract", lambda data, filename: _FakeEx())
    monkeypatch.setattr(jobs.parser, "parse_text", lambda md, t: "parsed")
    monkeypatch.setattr(jobs.chunker, "build_chunks", lambda parsed: [chunk])

    staged: list[bool] = []

    def fake_compare(cur, *, owner_id, document_id, project, job_id, source_id, new_rows, fence=None):
        if fence is not None:
            fence(cur)                      # contracted call site: after locks, before any write
        staged.append(True)
        cur.execute(
            "insert into brd_revision (document_id, owner_id, project, base_revision_no, summary) "
            "values (%s,%s,%s,1,%s) returning id", (document_id, owner_id, project, Jsonb({})))
        return {"revision_id": cur.fetchone()[0], "summary": {}}

    monkeypatch.setattr(jobs.revise, "compare", fake_compare)

    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("insert into brd_embedding_cache (owner_id, model, input_hash, embedding) "
                        "values (%s,%s,%s,%s::vector)", (owner, model, embed_hash, _vec()))
            conn.commit()

        job = {"id": job_id, "owner_id": owner, "project": project, "document_id": doc_id,
               "source_id": source_id, "mode": "revision", "attempts": 1, "max_attempts": 4}
        jobs._run(job)             # Canceled is caught internally -- must not raise out

        assert staged == []        # the fake's write never ran: fence raised first
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select count(*) from brd_revision where document_id = %s", (doc_id,))
            assert cur.fetchone()[0] == 0
            cur.execute("select status from ingest_job where id = %s", (job_id,))
            assert cur.fetchone()[0] == "canceled"
    finally:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("delete from brd_revision where document_id = %s", (doc_id,))
            cur.execute("delete from brd_document where id = %s", (doc_id,))
            cur.execute("delete from brd_source where id = %s", (source_id,))
            cur.execute("delete from brd_embedding_cache where owner_id = %s and input_hash = %s",
                        (owner, embed_hash))
            conn.commit()


# --- 9. retry(): attempts is monotonic, never reset (no ABA on the fencing token) --------

def test_retry_never_resets_attempts():
    owner = _owner()
    project = f"zz-it-retrymono-{uuid.uuid4().hex[:8]}"
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("insert into ingest_job (owner_id, project, mode, status, attempts, max_attempts) "
                    "values (%s,%s,'revision','failed',4,4) returning id", (owner, project))
        job_id = cur.fetchone()[0]
        conn.commit()
    try:
        assert jobs.retry(owner, job_id) is True
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select status, attempts, max_attempts from ingest_job where id = %s", (job_id,))
            status, attempts, max_attempts = cur.fetchone()
        assert status == "queued"
        assert attempts == 4                                      # never reset to 0
        assert max_attempts == 4 + settings.ingest_max_attempts    # fresh budget, token only grows
    finally:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("delete from ingest_job where id = %s", (job_id,))
            conn.commit()


# --- 10. upload -> delete -> upload still counts toward the per-hour rate limit ----------

def test_upload_delete_upload_counts_toward_rate_limit():
    owner = _owner()
    original_limit = jobs.settings.ingest_uploads_per_hour
    object.__setattr__(jobs.settings, "ingest_uploads_per_hour", 1)
    started = datetime.now(timezone.utc)
    data = b"%PDF-1.7 fake pdf body"
    filename = f"zz-it-ratelimit-{uuid.uuid4().hex[:8]}.pdf"
    project = None
    try:
        result = jobs.submit(owner, owner, data, filename)
        assert result["ok"] is True
        project = result["project"]

        # simulate the BRD delete: brd_document cascades ingest_job; purge_project_rows
        # cleans up the rest. ingest_upload_log is untouched by either.
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("delete from brd_document where owner_id = %s and project = %s", (owner, project))
            jobs.purge_project_rows(cur, owner, project)
            conn.commit()

        with pytest.raises(jobs.UploadRejected) as exc_info:
            jobs.submit(owner, owner, data, filename)
        assert exc_info.value.status == 429
    finally:
        object.__setattr__(jobs.settings, "ingest_uploads_per_hour", original_limit)
        with pool().connection() as conn, conn.cursor() as cur:
            if project:
                cur.execute("delete from brd_document where owner_id = %s and project = %s", (owner, project))
                cur.execute("delete from brd_source where owner_id = %s and project = %s", (owner, project))
                cur.execute("delete from ingest_job where owner_id = %s and project = %s", (owner, project))
            cur.execute("delete from ingest_upload_log where owner_id = %s and created_at >= %s", (owner, started))
            conn.commit()


# --- 11. periodic vector sweep: age + reference aware, owner-wide -----------------------

def test_sweep_orphan_vectors_deletes_old_unreferenced_keeps_referenced_and_recent(monkeypatch):
    owner = _owner()
    monkeypatch.setattr(jobs, "_VECTOR_GC_MIN_AGE", 60)      # 60s, so "now()" counts as recent
    project = f"zz-it-vgc-{uuid.uuid4().hex[:8]}"
    h_old = f"zz-vgc-old-{uuid.uuid4().hex[:8]}"
    h_ref = f"zz-vgc-ref-{uuid.uuid4().hex[:8]}"
    h_recent = f"zz-vgc-recent-{uuid.uuid4().hex[:8]}"
    vec = _vec()
    doc_id = None
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("insert into brd_document (title, project, sha256, owner_id, context_title) "
                        "values (%s,%s,'x',%s,%s) returning id", (project, project, owner, project))
            doc_id = cur.fetchone()[0]
            cur.execute("insert into brd_chunk (document_id, section, ordinal, text, content_hash, embed_hash) "
                        "values (%s,'S',0,'t','zz-vgc-ch',%s)", (doc_id, h_ref))
            cur.execute("insert into brd_embedding_cache (owner_id, model, input_hash, embedding, last_used_at) "
                        "values (%s,%s,%s,%s::vector, now() - interval '1 hour')", (owner, MODEL, h_old, vec))
            cur.execute("insert into brd_embedding_cache (owner_id, model, input_hash, embedding, last_used_at) "
                        "values (%s,%s,%s,%s::vector, now() - interval '1 hour')", (owner, MODEL, h_ref, vec))
            cur.execute("insert into brd_embedding_cache (owner_id, model, input_hash, embedding, last_used_at) "
                        "values (%s,%s,%s,%s::vector, now())", (owner, MODEL, h_recent, vec))
            conn.commit()

        n = jobs.sweep_orphan_vectors()
        assert n >= 1
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select input_hash from brd_embedding_cache where owner_id = %s and input_hash = any(%s)",
                        (owner, [h_old, h_ref, h_recent]))
            left = {r[0] for r in cur.fetchall()}
        assert left == {h_ref, h_recent}       # referenced survives regardless of age; recent survives on age
        assert h_old not in left
    finally:
        with pool().connection() as conn, conn.cursor() as cur:
            if doc_id:
                cur.execute("delete from brd_document where id = %s", (doc_id,))
            cur.execute("delete from brd_embedding_cache where owner_id = %s and input_hash = any(%s)",
                        (owner, [h_old, h_ref, h_recent]))
            conn.commit()


# --- 12. object purge skips a just-re-put (fresh) object ---------------------------------

def test_purge_after_brd_delete_skips_recently_put_object():
    owner = _owner()
    key = f"brd-sources/{owner}/zz-it-purge-recent-{uuid.uuid4().hex[:8]}.pdf"
    st = storage.store()
    st.put(key, b"x", "application/pdf")
    try:
        res = jobs.purge_after_brd_delete(owner, keys=[key], hashes=None)
        assert res["objects_deleted"] == 0
        assert st.exists(key)          # just put -- too fresh to safely judge as abandoned
    finally:
        st.delete(key)


# --- 13. requirement edit mirrors its vector into brd_embedding_cache -------------------

def test_update_requirement_writes_embedding_cache(monkeypatch):
    from backend.requirements import edit

    owner = _owner()
    project = f"zz-it-editcache-{uuid.uuid4().hex[:8]}"
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("insert into brd_document (title, project, sha256, owner_id, context_title) "
                    "values (%s,%s,'x',%s,%s) returning id", (project, project, owner, project))
        doc_id = cur.fetchone()[0]
        cur.execute("insert into brd_chunk (document_id, section, ordinal, text, content_hash) "
                    "values (%s,'S',0,'original text','zz-ec-h0') returning id", (doc_id,))
        chunk_id = cur.fetchone()[0]
        conn.commit()

    fake_vec = [0.0] * 1024
    monkeypatch.setattr(edit, "embed_documents", lambda inputs: ([list(fake_vec) for _ in inputs], 0))
    try:
        res = edit.update_requirement(owner, chunk_id, "edited text")
        assert res["changed"] is True
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select embed_hash from brd_chunk where id = %s", (chunk_id,))
            embed_hash = cur.fetchone()[0]
            assert embed_hash
            cur.execute("select 1 from brd_embedding_cache where owner_id = %s and model = %s and input_hash = %s",
                        (owner, settings.embed_model_docs, embed_hash))
            assert cur.fetchone() is not None
    finally:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select embed_hash from brd_chunk where id = %s", (chunk_id,))
            r = cur.fetchone()
            if r and r[0]:
                cur.execute("delete from brd_embedding_cache where owner_id = %s and input_hash = %s",
                            (owner, r[0]))
            cur.execute("delete from brd_document where id = %s", (doc_id,))
            conn.commit()

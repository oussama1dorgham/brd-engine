"""Hardening around BRD delete/upload/revision endpoints:

  1. delete_brd is ONE transaction (BRD rows + conversations + use-case rows + ingest rows);
     a failure anywhere leaves everything in place and returns a clean JSON 500.
  2. DB contention (deadlock / lock timeout / other OperationalError) on the revision and
     requirement-edit endpoints returns a JSON 409/503, never a Starlette plain-text 500.
  3. /brd/revision/apply surfaces NeedsConfirmation as 409 {confirm_required, summary} and
     threads confirm_replace through to revise.apply_revision.
  4. /upload sanitizes X-Filename/X-Title (control chars, including a URL-decoded NUL).
  5. /projects' job object carries warnings/error; /brd/requirements?include_archived=1
     also returns brd_chunk_archive rows; /health carries a "storage" flag.

NOTE ON PARALLEL WORK: backend/ingest/jobs.py is being extended by another agent with
`brd_embed_hashes(cur, owner_id, project)`, a 3-arg `purge_after_brd_delete(owner_id, keys,
hashes)`, and `start_source_gc()` — none of which exist yet as of this test run. Tests that
exercise /delete_brd stub whatever is missing via `_ensure_jobs_contract()` below so they
test OUR code against the AGREED contract rather than against a half-landed dependency.
"""
import inspect

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from backend import api
from backend.accounts import auth
from backend.accounts.auth import require_user
from backend.core.db import pool
from backend.ingest import jobs as ingest_jobs

pytestmark = pytest.mark.integration


def ok(cond, msg):
    assert cond, msg
    print("PASS", msg)


def _client(owner, monkeypatch):
    me = {"id": owner, "email": "smoke@local", "email_verified": True}
    api.app.dependency_overrides[require_user] = lambda: me
    monkeypatch.setattr(auth, "current_user", lambda request: me)   # read endpoints use require_scope
    return TestClient(api.app)


def _ensure_jobs_contract(monkeypatch):
    """Stub the parts of the ingest.jobs contract (see module docstring) that haven't
    landed yet, so delete_brd's own logic is what's under test."""
    if not hasattr(ingest_jobs, "brd_embed_hashes"):
        monkeypatch.setattr(ingest_jobs, "brd_embed_hashes",
                            lambda cur, owner_id, project: [], raising=False)
    try:
        n_params = len(inspect.signature(ingest_jobs.purge_after_brd_delete).parameters)
    except (TypeError, ValueError):
        n_params = 3
    if n_params < 3:
        orig = ingest_jobs.purge_after_brd_delete
        monkeypatch.setattr(ingest_jobs, "purge_after_brd_delete",
                            lambda owner_id, keys, hashes=None: orig(owner_id, keys))
    if not hasattr(ingest_jobs, "start_source_gc"):
        monkeypatch.setattr(ingest_jobs, "start_source_gc", lambda: None, raising=False)


# --- 1. delete_brd atomicity -------------------------------------------------------------

def test_delete_brd_success_leaves_no_rows(sandbox, monkeypatch):
    _ensure_jobs_contract(monkeypatch)
    owner, project = sandbox["owner"], sandbox["project"]
    c = _client(owner, monkeypatch)
    r = c.post("/delete_brd", json={"project": project})
    ok(r.status_code == 200 and r.json().get("ok"), f"delete_brd succeeded: {r.text}")
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from brd_document where project = %s", (project,))
        ok(cur.fetchone()[0] == 0, "BRD rows gone")
        cur.execute("select count(*) from use_case where owner_id = %s and project = %s", (owner, project))
        ok(cur.fetchone()[0] == 0, "use-case rows gone")


def test_delete_brd_atomic_on_use_case_purge_failure(sandbox, monkeypatch):
    _ensure_jobs_contract(monkeypatch)
    owner, project = sandbox["owner"], sandbox["project"]
    c = _client(owner, monkeypatch)

    def boom(cur, owner_id, project_):
        raise RuntimeError("simulated use-case purge failure")

    monkeypatch.setattr(api.use_cases, "purge_project_rows", boom)
    r = c.post("/delete_brd", json={"project": project})
    ok(r.status_code == 500, f"failed purge -> JSON 500, got {r.status_code}: {r.text}")
    ok(r.json().get("error") == "Could not delete this BRD — nothing was changed.",
       f"500 body is a generic, non-leaking message: {r.text}")
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from brd_document where project = %s", (project, ))
        ok(cur.fetchone()[0] > 0, "BRD rows NOT deleted — the transaction rolled back")
        cur.execute("select count(*) from use_case where owner_id = %s and project = %s", (owner, project))
        ok(cur.fetchone()[0] > 0, "use-case rows NOT deleted either")


def test_delete_brd_refused_while_generation_running(sandbox, monkeypatch):
    _ensure_jobs_contract(monkeypatch)
    owner, project = sandbox["owner"], sandbox["project"]
    c = _client(owner, monkeypatch)
    monkeypatch.setattr(api, "_generation_running", lambda user, proj: True)

    r = c.post("/delete_brd", json={"project": project})
    ok(r.status_code == 409, f"delete refused while generating -> 409, got {r.status_code}: {r.text}")
    ok("error" in r.json(), "409 body carries an error message")
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from brd_document where project = %s", (project,))
        ok(cur.fetchone()[0] > 0, "BRD NOT deleted while generation is running")


def test_delete_brd_removes_service_account_grants(sandbox, monkeypatch):
    _ensure_jobs_contract(monkeypatch)
    owner, project = sandbox["owner"], sandbox["project"]
    c = _client(owner, monkeypatch)
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("insert into service_account (owner_id, name) values (%s, 'zz-it-sa') returning id", (owner,))
        sa_id = cur.fetchone()[0]
        cur.execute("insert into service_account_project (service_account_id, project) values (%s, %s)",
                    (sa_id, project))
        conn.commit()

    r = c.post("/delete_brd", json={"project": project})
    ok(r.status_code == 200 and r.json().get("ok"), f"delete_brd succeeded: {r.text}")
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from service_account_project where service_account_id = %s and project = %s",
                    (sa_id, project))
        ok(cur.fetchone()[0] == 0, "the grant for the deleted project is gone")
        cur.execute("delete from service_account where id = %s", (sa_id,))
        conn.commit()


# --- 2. DB contention -> JSON 409/503, never a plain-text 500 ---------------------------

def test_requirement_update_deadlock_is_json_409(sandbox, monkeypatch):
    owner, chunk_id = sandbox["owner"], sandbox["chunks"][0]
    c = _client(owner, monkeypatch)

    def boom(*a, **k):
        raise psycopg.errors.DeadlockDetected("deadlock detected")

    monkeypatch.setattr(api, "update_requirement", boom)
    r = c.post("/brd/requirement/update", json={"chunk_id": chunk_id, "new_text": "x"})
    ok(r.status_code == 409, f"deadlock -> 409, got {r.status_code}: {r.text}")
    ok(r.json()["error"] == "The BRD is busy — try again in a moment.", "clean busy message")


def test_requirement_update_operational_error_is_json_503(sandbox, monkeypatch):
    owner, chunk_id = sandbox["owner"], sandbox["chunks"][0]
    c = _client(owner, monkeypatch)

    def boom(*a, **k):
        raise psycopg.OperationalError("connection lost")

    monkeypatch.setattr(api, "update_requirement", boom)
    r = c.post("/brd/requirement/update", json={"chunk_id": chunk_id, "new_text": "x"})
    ok(r.status_code == 503, f"other operational error -> 503, got {r.status_code}: {r.text}")
    ok(r.json()["error"] == "The BRD is busy — try again in a moment.", "clean busy message")


def test_revision_discard_deadlock_is_json_409(sandbox, monkeypatch):
    owner = sandbox["owner"]
    c = _client(owner, monkeypatch)

    def boom(*a, **k):
        raise psycopg.errors.LockNotAvailable("lock not available")

    monkeypatch.setattr(api.brd_revise, "discard_revision", boom)
    r = c.post("/brd/revision/discard", json={"revision_id": 1})
    ok(r.status_code == 409, f"lock-not-available -> 409, got {r.status_code}: {r.text}")
    ok(r.json()["error"] == "The BRD is busy — try again in a moment.", "clean busy message")


# --- 3. NeedsConfirmation + confirm_replace pass-through ---------------------------------

class _FakeNeedsConfirmation(Exception):
    """Fallback only if backend.ingest.revise.NeedsConfirmation hasn't landed yet."""
    def __init__(self, summary):
        self.summary = summary
        super().__init__("This looks like a wholesale replacement — confirm to proceed.")


def test_revision_apply_needs_confirmation_then_confirm_replace(sandbox, monkeypatch):
    owner, project = sandbox["owner"], sandbox["project"]
    c = _client(owner, monkeypatch)
    if not hasattr(api.brd_revise, "NeedsConfirmation"):
        monkeypatch.setattr(api.brd_revise, "NeedsConfirmation", _FakeNeedsConfirmation, raising=False)
    calls = []

    def fake_apply(owner_id, revision_id, *, keep_removed=None, skip=None, by=None, confirm_replace=False):
        calls.append(confirm_replace)
        if not confirm_replace:
            raise api.brd_revise.NeedsConfirmation(
                {"same": 1, "modified": 2, "added": 0, "removed": 20, "live": 22,
                 "matched": 2, "matched_ratio": 0.09},
            )
        return {"project": project, "applied": True}

    monkeypatch.setattr(api.brd_revise, "apply_revision", fake_apply)

    r1 = c.post("/brd/revision/apply", json={"revision_id": 1})
    ok(r1.status_code == 409, f"unconfirmed replacement -> 409, got {r1.status_code}: {r1.text}")
    body = r1.json()
    ok(body.get("confirm_required") is True, "confirm_required flag set")
    ok(body.get("summary", {}).get("removed") == 20, "summary passed through")

    r2 = c.post("/brd/revision/apply", json={"revision_id": 1, "confirm_replace": True})
    ok(r2.status_code == 200 and r2.json().get("ok"), f"confirmed replacement -> 200: {r2.text}")
    ok(calls == [False, True], "confirm_replace threaded through to apply_revision both times")


# --- 4. /upload header sanitization -------------------------------------------------------

def test_upload_nul_in_filename_is_not_a_500(monkeypatch):
    me = {"id": 1, "email": "smoke@local", "email_verified": True}
    api.app.dependency_overrides[require_user] = lambda: me
    c = TestClient(api.app)
    captured = {}

    def fake_submit(owner_id, by, data, filename, *, title=None, target_project=None, as_new=False):
        captured["filename"] = filename
        captured["title"] = title
        return {"ok": True, "job_id": 1}

    monkeypatch.setattr(api.ingest_jobs, "submit", fake_submit)
    r = c.post("/upload", data=b"not a real docx",
              headers={"X-Filename": "report%00name.docx", "X-Title": "My%00Title"})
    ok(r.status_code != 500, f"NUL in headers must not 500, got {r.status_code}: {r.text}")
    ok("\x00" not in captured.get("filename", "\x00"), "NUL stripped from filename before it reached submit()")
    ok("\x00" not in (captured.get("title") or "\x00"), "NUL stripped from title too")


def test_upload_missing_filename_after_sanitizing_falls_back(monkeypatch):
    me = {"id": 1, "email": "smoke@local", "email_verified": True}
    api.app.dependency_overrides[require_user] = lambda: me
    c = TestClient(api.app)
    # A filename that is ONLY control characters sanitizes to empty -> same as missing today.
    r = c.post("/upload", data=b"x", headers={"X-Filename": "%00%00%00"})
    ok(r.status_code == 400, f"empty-after-sanitize filename -> 400 like a missing one, got {r.status_code}")


# --- 5. /projects warnings, /brd/requirements archived, /health storage -----------------

def test_projects_job_carries_warnings_and_error(sandbox, monkeypatch):
    owner, project, doc = sandbox["owner"], sandbox["project"], sandbox["doc"]
    c = _client(owner, monkeypatch)
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into ingest_job (owner_id, project, document_id, mode, status, error, warnings) "
            "values (%s, %s, %s, 'new', 'failed', 'boom', %s)",
            (owner, project, doc, Jsonb(["low-confidence OCR on page 2"])),
        )
        conn.commit()
    r = c.get("/projects")
    ok(r.status_code == 200, "projects listing ok")
    row = next(p for p in r.json()["projects"] if p["project"] == project)
    job = row.get("job") or {}
    ok(job.get("error") == "boom", f"job.error carried through: {job}")
    ok(job.get("warnings") == ["low-confidence OCR on page 2"], f"job.warnings carried through: {job}")


def test_brd_requirements_include_archived(sandbox, monkeypatch):
    owner, project, doc = sandbox["owner"], sandbox["project"], sandbox["doc"]
    c = _client(owner, monkeypatch)
    archive_id = doc * 100000 + 7
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into brd_chunk_archive (id, document_id, req_id, section, text, content_hash, reason) "
            "values (%s, %s, 'FR-9', 'Old section', 'An archived requirement.', 'archhash-it', 'edit')",
            (archive_id, doc),
        )
        conn.commit()

    default = c.get(f"/brd/requirements?project={project}").json()["requirements"]
    ok(not any(row.get("archived") for row in default), "archived rows absent by default")

    withall = c.get(f"/brd/requirements?project={project}&include_archived=1").json()["requirements"]
    archived = [row for row in withall if row.get("archived")]
    ok(len(archived) == 1 and archived[0]["chunk_id"] == archive_id, "archived requirement returned")
    ok(bool(archived[0].get("retired_at")), "archived requirement carries retired_at")


def test_health_has_storage_flag():
    c = TestClient(api.app)
    r = c.get("/health")
    body = r.json()
    ok("storage" in body, f"/health carries a storage flag: {body}")
    ok(isinstance(body["storage"], bool), "storage flag is a bool")


def test_health_responds_fast_even_if_storage_is_down(monkeypatch):
    """The storage probe is refreshed by a background thread, not on the request path — even
    a cached-down reading must not make /health slow (the real S3 client's retries can take
    ~17s against a dead endpoint, which would fail a 5s Docker healthcheck)."""
    import time as _time
    monkeypatch.setitem(api._storage_health, "ok", False)
    c = TestClient(api.app)
    t0 = _time.monotonic()
    r = c.get("/health")
    elapsed = _time.monotonic() - t0
    ok(elapsed < 1.0, f"/health never blocks on the storage probe: {elapsed:.2f}s")
    ok(r.json()["storage"] is False, "the cached flag is reflected, not re-probed synchronously")


# --- 6. header sanitization: extension survival + bidi stripping ------------------------

def test_sanitize_header_keeps_extension_when_truncating():
    raw = ("a" * 250) + ".docx"
    cleaned = api._sanitize_header(raw)
    ok(len(cleaned) <= api._HEADER_MAX, f"capped at {api._HEADER_MAX} chars: len={len(cleaned)}")
    ok(cleaned.endswith(".docx"), f"extension survives truncation (stem is cut instead): {cleaned[-10:]!r}")


def test_sanitize_header_strips_bidi_controls():
    raw = "report‮cod.exe⁦.pdf"
    cleaned = api._sanitize_header(raw)
    ok("‮" not in cleaned and "⁦" not in cleaned, f"bidi override/isolate controls stripped: {cleaned!r}")

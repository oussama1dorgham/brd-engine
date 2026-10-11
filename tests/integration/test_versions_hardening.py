"""Step 6 hardening: restoring a version into the draft, guards while generation runs, the version
endpoints over HTTP, and test evidence in object storage (skipped when the store is down)."""
import psycopg
import pytest
from fastapi.testclient import TestClient

from backend import api
from backend.accounts import auth
from backend.accounts.auth import require_user
from backend.core import storage
from backend.core.db import pool
from backend.use_cases import generation as ucs, suites, testing as uct

pytestmark = pytest.mark.integration


def ok(cond, msg):
    assert cond, msg
    print("PASS", msg)


def _client(owner, monkeypatch):
    me = {"id": owner, "email": "smoke@local", "email_verified": True}
    api.app.dependency_overrides[require_user] = lambda: me
    monkeypatch.setattr(auth, "current_user", lambda request: me)      # read endpoints use require_scope
    return TestClient(api.app)


def test_restore_version_and_guards(sandbox, monkeypatch):
    owner, project = sandbox["owner"], sandbox["project"]
    pay, declined, refund = sandbox["cases"]
    c = _client(owner, monkeypatch)
    try:
        uct.record(owner, refund["id"], status="failed")                    # test state must survive a restore
        ucs.update_use_case(owner, pay["id"], {"title": "Pay by card v2", "steps": ["x"]})
        ucs.delete_use_case(owner, refund["id"])
        st = c.get(f"/use-cases/versions?project={project}").json()
        v2 = c.post("/use-cases/versions/publish",
                    json={"project": project, "label": "bad", "expect": st["fingerprint"]}).json()
        ok(v2["ok"] and v2["version"]["number"] == 2, "v2 published over HTTP")
        stale = c.post("/use-cases/versions/publish", json={"project": project, "expect": "not-the-fingerprint"})
        ok(stale.status_code in (200, 409), "publish with a stale fingerprint handled")
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("update brd_chunk set text = 'The customer can pay by card or cash.' where id = %s",
                        (sandbox["chunks"][0],))
            conn.commit()

        # guard: nothing may publish / restore while a generation job writes the draft
        key = api._uc_key({"id": owner}, project)
        api._uc_jobs[key] = {"running": True}
        try:
            ok(c.post("/use-cases/versions/publish", json={"project": project}).status_code == 409,
               "publish refused while generation runs")
            ok(c.post("/use-cases/versions/restore", json={"project": project, "version_id": sandbox["v1"]}).status_code
               == 409, "restore refused while generation runs")
        finally:
            api._uc_jobs.pop(key, None)

        r = c.post("/use-cases/versions/restore", json={"project": project, "version_id": sandbox["v1"]}).json()
        ok(r["ok"] and (r["updated"], r["restored"], r["removed"]) == (1, 1, 0), "restore v1: 1 rewritten, 1 re-inserted")
        ok(r["requirements_changed_since"] == 1, "…and the requirement edited since is flagged")
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select uid, uc_id, title, test_status from use_case where owner_id=%s and project=%s", (owner, project))
            rows = {u: (code, t, s) for u, code, t, s in cur.fetchall()}
        ok(rows[pay["uid"]][:2] == ("UC-1", "Pay by card"), "UC-1 is back to v1, same uid and code")
        ok(rows[refund["uid"]] == ("UC-3", "Request a refund", "failed"), "UC-3 re-inserted with its uid, code and verdict")
        ok(not suites.diff(owner, project, sandbox["v1"], "draft")["summary"]["modified"],
           "the draft now equals v1's definitions")
        ok(ucs.resume_state(owner, project)["changed"] == 1, "Update now offers the changed requirement")
        v3 = suites.publish(owner, project, owner, "Restore of v1")["version"]
        ok(v3["number"] == 3, "the restore publishes as a NEW version (v2 is kept)")

        ok(c.get(f"/use-cases/versions/diff?project={project}&base=999999&target=draft").status_code == 404,
           "diff of an unknown version → 404")
        ok(c.get(f"/use-cases/versions/{sandbox['v1']}/case/999999?project={project}").status_code == 404,
           "unknown case in a version → 404")
        other = c.get("/use-cases/versions?project=zz-not-mine")
        ok(other.status_code in (200, 403, 404) and not (other.json().get("versions") or []),
           "another project shows no versions")
    finally:
        api.app.dependency_overrides.pop(require_user, None)


def test_proof_files_in_object_storage(sandbox):
    try:
        storage.store().keys(uct.PROOF_PREFIX)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"object storage unavailable: {e}")
    owner, project = sandbox["owner"], sandbox["project"]
    pay = sandbox["cases"][0]
    p = uct.stage_proof(owner, pay["id"], filename="shot.png", mime="image/png", content=b"\x89PNG-bytes")
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select object_key, content is null from use_case_test_proof where id = %s", (p["id"],))
        key, no_bytes = cur.fetchone()
    ok(key and key.startswith(uct.PROOF_PREFIX) and no_bytes, "a new file lands in object storage, not Postgres")
    ok(uct.get_proof_blob(owner, p["id"])[0] == b"\x89PNG-bytes", "…and is served from there")
    ok(uct.delete_proof(owner, p["id"]) and not storage.store().exists(key), "deleting it removes the object")

    with pool().connection() as conn, conn.cursor() as cur:             # an old upload, still in Postgres
        cur.execute("insert into use_case_test_proof (use_case_uid, owner_id, project, kind, filename, mime, size, "
                    "content, ordinal) values (%s,%s,%s,'image','old.png','image/png',3,%s,0) returning id",
                    (pay["uid"], owner, project, psycopg.Binary(b"old")))
        old = cur.fetchone()[0]
        conn.commit()
    ok(uct.move_blobs_to_storage() >= 1 and uct.get_proof_blob(owner, old)[0] == b"old", "old DB blob moved, still served")
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select object_key from use_case_test_proof where id = %s", (old,))
        moved_key = cur.fetchone()[0]
    ucs.purge_project(owner, project)                                  # what deleting the BRD does
    ok(not storage.store().exists(moved_key), "a BRD purge removes its proof objects too")

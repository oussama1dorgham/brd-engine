"""HTTP-level hardening of the test-execution endpoints: upload caps, sandboxed proof serving,
link validation, cycle + bulk + undo via the API. Auth is overridden to the card's owner."""
from fastapi.testclient import TestClient
from backend import api
from backend.accounts.auth import require_user
from backend.core.db import pool
from backend.use_cases import testing as uct

import pytest

pytestmark = pytest.mark.integration


def ok(cond, msg):
    assert cond, msg
    print('PASS', msg)


def test_use_case_testing_http(sandbox):
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select id, uid, owner_id, project from use_case where test_status='not_tested' and project = %s "
                    "order by id limit 1", (sandbox["project"],))
        pk, uid, owner, project = cur.fetchone()
        cur.execute("select coalesce(max(id),0) from use_case_test_result"); max_res = cur.fetchone()[0]
        cur.execute("select coalesce(max(id),0) from use_case_audit"); max_aud = cur.fetchone()[0]
    api.app.dependency_overrides[require_user] = lambda: {"id": owner, "email": "smoke@local"}
    c = TestClient(api.app)
    cycle_id = None
    try:
        big = c.post("/use-cases/proof/file", content=b"x" * 10,
                     headers={"X-UC-Id": str(pk), "Content-Type": "image/png", "Content-Length": str(6 * 1024 * 1024)})
        ok(big.status_code == 413, f"declared oversize refused before buffering ({big.status_code})")
        html = c.post("/use-cases/proof/file", content=b"<script>alert(1)</script>",
                      headers={"X-UC-Id": str(pk), "Content-Type": "text/html", "X-Filename": "evil.html"}).json()
        ok(html["proof"]["kind"] == "file" and html["proof"]["result_id"] is None, "html upload staged as a plain file")
        r = c.get(f"/use-cases/proof/{html['proof']['id']}")
        ok(r.headers["content-type"] == "application/octet-stream"
           and r.headers["content-disposition"].startswith("attachment")
           and r.headers["x-content-type-options"] == "nosniff"
           and "sandbox" in r.headers["content-security-policy"], "html proof served as a sandboxed download")
        png = c.post("/use-cases/proof/file", content=b"\x89PNG\r\n\x1a\nxx",
                     headers={"X-UC-Id": str(pk), "Content-Type": "image/png", "X-Kind": "file", "X-Filename": "shot%20%C3%A9.png"}).json()
        ok(png["proof"]["kind"] == "image" and png["proof"]["filename"] == "shot é.png", "kind derived from mime; utf-8 name kept")
        r = c.get(f"/use-cases/proof/{png['proof']['id']}")
        ok(r.headers["content-type"] == "image/png" and r.headers["content-disposition"].startswith("inline")
           and "immutable" in r.headers["cache-control"], "image served inline + immutable cache")
        js = c.post("/use-cases/proof/link", json={"id": pk, "url": "javascript:alert(1)"})
        ok(js.status_code == 400, "javascript: link rejected")
        cyc = c.post("/test-cycles/create", json={"project": project, "name": "HTTP smoke", "source": "empty"})
        if cyc.status_code == 200:
            cycle_id = cyc.json()["cycle"]["id"]
        else:   # enforce_project needs a real request context → create directly
            cycle_id = uct.create_cycle(owner, project, "HTTP smoke", source="empty")["id"]
        rec = c.post("/use-cases/test-status", json={"id": pk, "status": "failed", "note": "boom",
                                                     "defect_ref": "https://jira.example/BUG-9", "cycle_id": cycle_id}).json()
        ok(rec["ok"] and rec["claimed"] == 2 and rec["card"]["cycle"]["status"] == "failed", "verdict via HTTP claims staged proofs into cycle")
        st = c.get(f"/test-cycles/{cycle_id}/statuses").json()
        ok(st["cases"][str(uid)]["defect_ref"] == "https://jira.example/BUG-9", "cycle statuses carry the defect")
        bulk = c.post("/use-cases/test-status/bulk", json={"ids": [pk], "status": "passed", "cycle_id": cycle_id}).json()
        ok(bulk["cards"][0]["cycle"]["status"] == "passed", "bulk verdict")
        tl = c.get(f"/use-cases/{uid}/timeline?cycle_id={cycle_id}").json()
        ok(len(tl["events"]) == 2 and tl["events"][0]["undoable"] and not tl["events"][1]["latest"], "timeline flags latest/undoable")
        und = c.post("/use-cases/test-status/undo", json={"result_id": tl["events"][0]["result_id"], "cycle_id": cycle_id}).json()
        ok(und["card"]["cycle"]["status"] == "failed", "undo bulk verdict via HTTP")
        closed = c.post("/test-cycles/update", json={"cycle_id": cycle_id, "status": "closed"}).json()
        ok(closed["cycle"]["status"] == "closed" and closed["cycle"]["closed_at"], "close cycle")
        rej = c.post("/use-cases/test-status", json={"id": pk, "status": "passed", "cycle_id": cycle_id})
        ok(rej.status_code == 400, "closed cycle rejects via HTTP")
        ok(c.post("/test-cycles/delete", json={"cycle_id": cycle_id}).json()["ok"], "delete cycle")
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select count(*) from use_case_test_result where use_case_uid=%s and id>%s and cycle_id is null", (uid, max_res))
            ok(cur.fetchone()[0] >= 1, "results kept as ad-hoc after cycle delete")
        cycle_id = None
    finally:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("delete from use_case_audit where use_case_uid=%s and id > %s", (uid, max_aud))
            cur.execute("delete from use_case_test_proof where use_case_uid=%s", (uid,))
            cur.execute("delete from use_case_test_result where use_case_uid=%s and id > %s", (uid, max_res))
            if cycle_id:
                cur.execute("delete from test_cycle where id=%s", (cycle_id,))
            uct.resync_card(cur, uid, owner)
            conn.commit()
        print("cleaned up")

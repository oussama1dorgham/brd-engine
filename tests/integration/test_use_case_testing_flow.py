"""Use-case test execution end to end on a real card: staging, verdicts, amend/undo, content-hash
retest clock, scope, cycles, report, restore-keeps-verdict. Restores the card afterwards."""
from backend.core.db import pool
from backend.use_cases import testing as uct
from backend.use_cases import versioning as ucv
from backend.use_cases import generation as ucs

import pytest

pytestmark = pytest.mark.integration


def ok(cond, msg):
    assert cond, msg
    print('PASS', msg)


def test_use_case_testing_flow(sandbox):
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select id, uid, owner_id, project, test_status, in_scope, steps from use_case "
                    "where test_status = 'not_tested' and project = %s order by id limit 1", (sandbox["project"],))
        pk, uid, owner, project, st0, sc0, steps0 = cur.fetchone()
        cur.execute("select coalesce(max(id),0) from use_case_test_result"); max_res = cur.fetchone()[0]
        cur.execute("select coalesce(max(id),0) from use_case_audit"); max_aud = cur.fetchone()[0]
        cur.execute("select coalesce(max(id),0) from use_case_version"); max_ver = cur.fetchone()[0]
    print("card", pk, uid, project)
    cycle = None
    try:
        # staging: evidence first, then the verdict claims it
        p1 = uct.stage_proof(owner, pk, filename="../evil\"name.png", mime="image/png; x=1", content=b"\x89PNG....")
        ok(p1["result_id"] is None and p1["kind"] == "image" and p1["filename"] == "evilname.png", "staged proof pending + sanitized")
        p2 = uct.stage_proof(owner, pk, filename="x.svg", mime="image/svg+xml", content=b"<svg/>")
        ok(p2["kind"] == "file", "svg is a file, not an image")
        try:
            uct.stage_proof(owner, pk, url="javascript:alert(1)"); ok(False, "js link rejected")
        except uct.TestingError:
            ok(True, "js link rejected")
        cycle = uct.create_cycle(owner, project, "Smoke cycle", source="empty")
        ok(cycle["stats"]["total"] == 0, "empty cycle")
        r = uct.record(owner, pk, status="failed", note="broke", defect_ref="BUG-1", cycle_id=cycle["id"])
        ok(r["claimed"] == 2 and not r["amended"] and r["card"]["test_status"] == "failed", "verdict claims staged proofs")
        ok(r["card"]["cycle"]["in_cycle"] and r["card"]["cycle"]["status"] == "failed", "auto-joined cycle")
        ok(len(r["event"]["proofs"]) == 2 and r["event"]["defect_ref"] == "BUG-1", "event carries proofs + defect")
        r2 = uct.record(owner, pk, status="passed", cycle_id=cycle["id"])
        ok(r2["amended"] and r2["event"]["amended_from"] == "failed" and r2["event"]["defect_ref"] is None, "in-window re-verdict amends, clears defect")
        u = uct.undo(owner, r2["event"]["result_id"], cycle_id=cycle["id"])
        ok(u["reverted"] and u["card"]["test_status"] == "failed", "undo reverts amend")
        # a note with only a staged link
        uct.stage_proof(owner, pk, url="https://example.com/log")
        n = uct.record(owner, pk, event_kind="note")
        ok(n["claimed"] == 1 and n["card"]["test_status"] == "failed", "attachment-only note keeps verdict")
        un = uct.undo(owner, n["event"]["result_id"])
        tl = uct.timeline(owner, uid, cycle["id"])
        ok(not un["reverted"] and len(tl["pending"]) == 1, "undo of a fresh note returns proof to staging")
        ok(uct.delete_proof(owner, p1["id"]), "proof on latest event deletable")
        uct.record(owner, pk, event_kind="note", note="later note")      # p2's event is now in the past
        try:
            uct.delete_proof(owner, p2["id"]); ok(False, "past-event proof locked")
        except uct.TestingError:
            ok(True, "past-event proof locked")
        # retest clock: a move must NOT trip it; a steps edit must
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select folder_id from use_case where id=%s", (pk,)); fid = cur.fetchone()[0]
        ucs.move_use_case(owner, pk, fid)
        ok(not uct.timeline(owner, uid)["card"]["needs_retest"], "move does not raise needs-retest")
        ucs.update_use_case(owner, pk, {"steps": list(steps0 or []) + ["extra smoke step"]})
        t2 = uct.timeline(owner, uid, cycle["id"])
        ok(t2["card"]["needs_retest"], "steps edit raises needs-retest overall (the draft moved on)")
        ok(not t2["card"]["cycle"]["needs_retest"] and t2["card"]["cycle"]["draft_differs"],
           "…but not inside the cycle: it tests v1's definition, which didn't change — flagged 'draft differs'")
        ucs.update_use_case(owner, pk, {"steps": list(steps0 or [])})
        ok(not uct.timeline(owner, uid)["card"]["needs_retest"], "restoring identical definition clears it")
        # scope + report
        s = uct.set_scope(owner, pk, False)
        ok(s["card"]["in_scope"] is False, "out of scope")
        rep = uct.report(owner, project, cycle["id"])
        ok(rep["out_of_scope"] == 1 and rep["total"] == 0, "cycle report excludes out-of-scope")
        uct.set_scope(owner, pk, True)
        rep = uct.report(owner, project, cycle["id"])
        ok(rep["total"] == 1 and rep["by_status"].get("failed") == 1 and len(rep["defects"]) == 1, "cycle report counts + defects")
        ok(any(q["coverage"] == "failed" for q in rep["requirements"]), "requirement coverage = failed")
        full = uct.report(owner, project)
        ok(full["total"] > 1 and "requirements" in full, f"overall report ({full['total']} cases, {len(full['requirements'])} reqs)")
        # B2: delete -> restore keeps the verdict
        ucs.delete_use_case(owner, pk)
        h = ucv.history(owner, uid)
        ucv.restore(owner, uid, h["versions"][0]["version_no"])
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select id, test_status from use_case where uid=%s", (uid,)); pk, st = cur.fetchone()
        ok(st == "failed", "restore from Trash keeps the verdict (B2)")
        cs = uct.cycle_statuses(owner, cycle["id"])
        ok(cs["cases"][str(uid)]["status"] == "failed", "cycle statuses overlay")
        cl = uct.list_cycles(owner, project)
        ok(any(c["id"] == cycle["id"] and c["stats"]["executed"] == 1 for c in cl), "list cycles with stats")
        uct.update_cycle(owner, cycle["id"], status="closed")
        try:
            uct.record(owner, pk, status="passed", cycle_id=cycle["id"]); ok(False, "closed cycle rejects")
        except uct.TestingError:
            ok(True, "closed cycle rejects results")
    finally:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("delete from use_case_audit where use_case_uid=%s and id > %s", (uid, max_aud))
            cur.execute("delete from use_case_test_proof where use_case_uid=%s", (uid,))
            cur.execute("delete from use_case_test_result where use_case_uid=%s and id > %s", (uid, max_res))
            cur.execute("delete from use_case_version where use_case_uid=%s and id > %s", (uid, max_ver))
            if cycle:
                cur.execute("delete from test_cycle where id=%s", (cycle["id"],))
            uct.resync_card(cur, uid, owner)
            cur.execute("select test_status, in_scope from use_case where uid=%s", (uid,))
            print("cleanup ->", cur.fetchone(), "(was", st0, sc0, ")")
            conn.commit()

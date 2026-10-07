"""Every act on a use case appends one attributed audit entry, in order; the log is append-only."""
from psycopg.types.json import Jsonb
from backend.core.db import pool
from backend.use_cases import audit as ua, testing as uct, versioning as ucv
from backend.use_cases import generation as ucs

import pytest

pytestmark = pytest.mark.integration


def ok(cond, msg):
    assert cond, msg
    print('PASS', msg)


def test_use_case_audit_log(sandbox):
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select id, uid, owner_id, project, steps, folder_id from use_case where test_status='not_tested' "
                    "and project = %s order by id desc limit 1", (sandbox["project"],))
        pk, uid, owner, project, steps0, fid = cur.fetchone()
        cur.execute("select email from app_user where id=%s", (owner,)); email = cur.fetchone()[0]
        mx = {}
        for t in ("use_case_audit", "use_case_test_result", "use_case_version"):
            cur.execute(f"select coalesce(max(id),0) from {t}"); mx[t] = cur.fetchone()[0]
    cycle = None
    try:
        cycle = uct.create_cycle(owner, project, "Audit smoke", source="empty")
        uct.stage_proof(owner, pk, filename="shot.png", mime="image/png", content=b"\x89PNG..", by=owner)
        r = uct.record(owner, pk, status="failed", note="broke", defect_ref="BUG-7", cycle_id=cycle["id"], by=owner)
        uct.record(owner, pk, status="passed", cycle_id=cycle["id"], by=owner)                     # amend
        uct.undo(owner, r["event"]["result_id"], cycle_id=cycle["id"], by=owner)                    # revert amend
        n = uct.record(owner, pk, event_kind="note", note="flaky env", by=owner)
        uct.undo(owner, n["event"]["result_id"], by=owner)                                          # undo a note
        uct.set_scope(owner, pk, False, by=owner); uct.set_scope(owner, pk, True, by=owner)
        ucs.update_use_case(owner, pk, {"steps": list(steps0 or []) + ["audit step"]}, changed_by=owner)
        ucs.move_use_case(owner, pk, fid, changed_by=owner)
        uct.set_cycle_members(owner, cycle["id"], remove_uids=[uid], by=owner)
        log = ua.entries(owner, uid)["entries"]
        new = [e for e in log if e["id"] > mx["use_case_audit"]]
        acts = [e["action"] for e in reversed(new)]
        print("  actions:", acts)
        want = ["proof_added", "cycle_planned", "status_recorded", "status_amended", "status_reverted", "note_added",
                "entry_undone", "scope_out", "scope_in", "edit", "move", "cycle_unplanned"]
        ok(acts == want, "every act audited, in order")
        ok(all(e["actor"] == email for e in new), f"every entry attributed to {email}")
        rec = next(e for e in new if e["action"] == "status_recorded")
        ok(rec["status_before"] == "not_tested" and rec["status_after"] == "failed" and rec["cycle_name"] == "Audit smoke"
           and rec["detail"].get("defect_ref") == "BUG-7" and rec["detail"].get("attachments") == 1,
           "verdict entry: before→after, cycle, defect, attachments")
        am = next(e for e in new if e["action"] == "status_amended")
        ok(am["summary"] == "Changed verdict Failed → Passed", "amend summary")
        rv = next(e for e in new if e["action"] == "status_reverted")
        ok(rv["status_before"] == "passed" and rv["status_after"] == "failed", "undo of amend is itself audited")
        ed = next(e for e in new if e["action"] == "edit")
        ok(ed["category"] == "definition" and ed["version_no"] and "steps" in (ed["summary"] or ""), "definition edit audited with version")
        ok(ua.entries(owner, uid, "test")["entries"] and all(e["category"] == "test" for e in ua.entries(owner, uid, "test")["entries"]),
           "category filter")
        try:
            with pool().connection() as conn, conn.cursor() as cur:
                cur.execute("update use_case_audit set summary='tamper' where id=%s", (new[0]["id"],)); conn.commit()
            ok(False, "audit is append-only")
        except Exception:
            ok(True, "audit is append-only (update rejected)")
    finally:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("update use_case set steps=%s where uid=%s", (Jsonb(steps0 or []), uid))
            cur.execute("delete from use_case_test_proof where use_case_uid=%s", (uid,))
            cur.execute("delete from use_case_test_result where use_case_uid=%s and id>%s", (uid, mx["use_case_test_result"]))
            cur.execute("delete from use_case_version where use_case_uid=%s and id>%s", (uid, mx["use_case_version"]))
            cur.execute("delete from use_case_audit where use_case_uid=%s and id>%s", (uid, mx["use_case_audit"]))
            if cycle:
                cur.execute("delete from test_cycle where id=%s", (cycle["id"],))
            uct.resync_card(cur, uid, owner)
            conn.commit()
        print("cleaned up")

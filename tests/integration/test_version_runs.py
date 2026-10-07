"""Runs bound to versions (migration 034): a cycle tests one published version; verdicts pin what
they tested; a requirement edit under an unchanged case asks for a retest; a new cycle carries the
passes of unchanged cases; an older cycle can still be finished."""
import psycopg
import pytest

from backend.core.db import pool
from backend.use_cases import audit as ua, generation as gen, suites, testing as uct

pytestmark = pytest.mark.integration


def ok(cond, msg):
    assert cond, msg
    print("PASS", msg)


def _card(owner, uid, cycle_id=None):
    return uct.timeline(owner, uid, cycle_id)["card"]


def test_runs_bound_to_versions(sandbox):
    owner, project = sandbox["owner"], sandbox["project"]
    pay, declined, refund = sandbox["cases"]
    pay_req, refund_req = sandbox["chunks"]

    a = uct.create_cycle(owner, project, "Sprint 1")
    ok(a["version"]["number"] == 1 and a["stats"]["total"] == 3 and a["carried"] == 0, "cycle A runs v1, 3 cases")
    uct.record(owner, pay["id"], status="passed", cycle_id=a["id"])
    uct.record(owner, declined["id"], status="passed", cycle_id=a["id"])
    uct.record(owner, refund["id"], status="failed", cycle_id=a["id"], defect_ref="BUG-9")
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from use_case_test_result r join use_case_suite_member m "
                    "on m.content_id = r.content_id and m.suite_version_id = %s where r.cycle_id = %s",
                    (sandbox["v1"], a["id"]))
        ok(cur.fetchone()[0] == 3, "every verdict records the v1 content it tested")

    # G3 — the refund REQUIREMENT changes; the refund case's own text does not
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update brd_chunk set text = 'The customer can request a refund within 30 days.' where id = %s",
                    (refund_req,))
        cur.execute("update use_case set title = 'Card declined (renamed)' where uid = %s", (declined["uid"],))
        conn.commit()
    ok(_card(owner, refund["uid"])["needs_retest"], "requirement edited under an unchanged case → needs retest")
    ok(not _card(owner, declined["uid"])["needs_retest"], "a title-only edit does not")
    ok(not _card(owner, refund["uid"], a["id"])["cycle"]["needs_retest"]
       and _card(owner, refund["uid"], a["id"])["cycle"]["draft_differs"],
       "inside cycle A (v1) the verdict still holds for v1 — flagged 'draft differs'")

    v2 = suites.publish(owner, project, owner, "After refund change")["version"]
    a2 = uct.cycle_statuses(owner, a["id"])["cycle"]
    ok(v2["number"] == 2 and a2["outdated"] and a2["latest_version"] == 2, "cycle A is now outdated (v2 exists)")
    r = uct.record(owner, pay["id"], event_kind="note", note="finishing the old run", cycle_id=a["id"])
    ok(r["event"] is not None, "…and can still be finished")

    b = uct.create_cycle(owner, project, "Sprint 2")
    st = uct.cycle_statuses(owner, b["id"])["cases"]
    ok(b["version"]["number"] == 2 and b["carried"] == 2, "cycle B runs v2 and carries 2 unchanged passes")
    ok(st[str(pay["uid"])]["status"] == "passed" and st[str(pay["uid"])]["carried"]
       and st[str(declined["uid"])]["carried"], "UC-1 and the renamed UC-2 start as carried passes")
    ok(st[str(refund["uid"])]["status"] == "not_tested", "the failed case whose requirement changed starts Not run")
    carried = next(e for e in ua.entries(owner, pay["uid"], "test")["entries"] if e["action"] == "status_carried")
    ok("Carried Passed from “Sprint 1” — unchanged in v2" == carried["summary"] and carried["suite_version"] == 2
       and carried["detail"]["carried_from"], "the carry is audited, attributed, and points at the original result")

    full = uct.create_cycle(owner, project, "Full regression", carry=False)
    ok(full["carried"] == 0 and full["stats"]["executed"] == 0, "carry off = full regression, everything Not run")

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("insert into use_case (owner_id, project, folder_id, uc_id, title, steps, source_chunk_ids) "
                    "values (%s, %s, %s, 'UC-9', 'Unpublished case', '[\"s\"]', %s) returning id, uid",
                    (owner, project, sandbox["folder"], psycopg.types.json.Jsonb([pay_req])))
        new_pk, new_uid = cur.fetchone()
        conn.commit()
    with pytest.raises(uct.TestingError, match="isn't in v2"):
        uct.record(owner, new_pk, status="passed", cycle_id=b["id"])
    print("PASS a case added to the draft after v2 can't be recorded in a v2 cycle")
    ok(uct.set_cycle_members(owner, b["id"], add_uc_ids=[new_pk])["not_in_version"] == 1,
       "…nor planned into it")
    ok(uct.create_cycle(owner, project, "Sprint 3")["unpublished_excluded"] == 1,
       "a new cycle reports the unpublished case it left out")

    uct.record(owner, pay["id"], status="passed")                          # ad hoc, on the draft
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select exists (select 1 from use_case_suite_member m where m.suite_version_id = %s "
                    "and m.content_id = r.content_id) from use_case_test_result r "
                    "where r.use_case_uid = %s and r.cycle_id is null order by r.id desc limit 1", (v2["id"], pay["uid"]))
        ok(cur.fetchone()[0], "an ad-hoc verdict on an unchanged case links to v2's content")   # (shared with v1)

    old = suites.case_in_version(owner, project, sandbox["v1"], refund["uid"])
    ok(old["version"] == 1 and old["requirements"][0]["text"] == "The customer can request a refund.",
       "v1 still shows the refund requirement as it was")
    ok(gen.resume_state(owner, project)["changed"] == 1, "and the requirement change is pending an Update")

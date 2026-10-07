"""Version-aware reports + run-over-run comparison (step 5): a cycle's report stays true to the
version it tested after the draft moves on; every report carries its version header and what
changed since the previous version; comparing two runs classifies regressions and their cause."""
import pytest

from backend.core.db import pool
from backend.use_cases import generation as ucs, suites, testing as uct

pytestmark = pytest.mark.integration


def ok(cond, msg):
    assert cond, msg
    print("PASS", msg)


def test_version_reports_and_regressions(sandbox):
    owner, project = sandbox["owner"], sandbox["project"]
    pay, declined, refund = sandbox["cases"]

    a = uct.create_cycle(owner, project, "Release 1")
    uct.record(owner, pay["id"], status="passed", cycle_id=a["id"])
    uct.record(owner, declined["id"], status="passed", cycle_id=a["id"])
    uct.record(owner, refund["id"], status="failed", cycle_id=a["id"], defect_ref="BUG-3")

    ucs.update_use_case(owner, pay["id"], {"steps": ["open checkout", "enter card", "confirm 3-D Secure", "submit"]})
    v2 = suites.publish(owner, project, owner, "3DS")["version"]
    b = uct.create_cycle(owner, project, "Release 2")
    uct.record(owner, pay["id"], status="failed", cycle_id=b["id"], defect_ref="BUG-4")
    uct.record(owner, refund["id"], status="passed", cycle_id=b["id"])

    # the draft moves on: the refund case is deleted, the card requirement is reworded
    ucs.delete_use_case(owner, refund["id"])
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update brd_chunk set text = 'The customer can pay by card (Visa or Mastercard).' where id = %s",
                    (sandbox["chunks"][0],))
        conn.commit()

    ra = uct.report(owner, project, a["id"])
    uids = {c["uid"]: c for c in ra["cases"]}
    ok(ra["version"]["number"] == 1 and ra["changes_since_previous"] is None, "report A: v1 header, no previous")
    ok(refund["uid"] in uids and not uids[refund["uid"]]["live"] and uids[refund["uid"]]["status"] == "failed",
       "report A still shows the refund case it tested (deleted from the draft since)")
    ok(not any(c["needs_retest"] for c in ra["cases"]), "…and nothing is stale against v1, whatever the draft did")

    rb = uct.report(owner, project, b["id"])
    ch = rb["changes_since_previous"]
    ok(rb["version"]["number"] == 2 and ch["base"] == 1 and ch["summary"]["modified"] == 1
       and ch["items"][0]["uid"] == pay["uid"] and ch["items"][0]["retest"], "report B: v2 header, UC-1 modified since v1")
    ok(rb["carried"] == 1 and {c["uid"]: c for c in rb["cases"]}[declined["uid"]]["carried"],
       "report B shows the carried pass")
    req = next(r for r in rb["requirements"] if r["chunk_id"] == sandbox["chunks"][0])
    ok(req["text"] == "The customer can pay by card." and req["changed_since_version"],
       "requirement shown as v2 had it, flagged as changed since")

    overall = uct.report(owner, project)
    ok(overall["version"]["number"] == 2 and overall["draft_changes"]["removed"] == 1,
       "overall report: latest version v2 + the unpublished removal")

    cmp = uct.compare_cycles(owner, project, a["id"], b["id"])
    by = {i["uid"]: i for i in cmp["items"]}
    ok(by[pay["uid"]]["change"] == "regressed" and by[pay["uid"]]["definition"] == "modified"
       and by[pay["uid"]]["retest"] and by[pay["uid"]]["defect_ref"] == "BUG-4",
       "UC-1 regressed — after its definition changed (steps)")
    ok(by[refund["uid"]]["change"] == "fixed" and by[refund["uid"]]["definition"] == "unchanged", "UC-3 fixed")
    ok(by[declined["uid"]]["change"] == "still_passing" and by[declined["uid"]]["carried"], "UC-2 still passing (carried)")
    ok(cmp["summary"]["regressed"] == 1 and cmp["summary"]["fixed"] == 1 and cmp["definition_changes"]["modified"] == 1,
       "comparison summary")
    ok(v2["number"] == 2, "sanity")

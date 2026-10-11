"""Incremental generation (migration 033): a requirement change updates ONLY the affected use
cases, in place (same uid + code), in the draft — never the published version. The LLM is a
scripted fake, so this runs offline against the local DB on a sandbox BRD."""
import json

import pytest

from backend.core.db import pool
from backend.use_cases import generation as gen, suites

pytestmark = pytest.mark.integration


def ok(cond, msg):
    assert cond, msg
    print("PASS", msg)


def _uc(title, cids, steps=("open checkout", "submit"), ctype="functional"):
    return {"case_type": ctype, "subtheme": "Cards", "title": title, "description": "", "roles": ["Customer"],
            "preconditions": "", "steps": list(steps), "expected_behaviour": "done", "source_chunk_ids": cids}


def test_incremental_update_in_place(sandbox, monkeypatch):
    owner, project = sandbox["owner"], sandbox["project"]
    pay_req, refund_req = sandbox["chunks"]
    pay, declined, refund = sandbox["cases"]
    calls = []
    script = {}

    def fake_llm(messages, **kw):
        prompt = messages[0]["content"]
        calls.append(prompt)
        if script.get("fail"):
            raise RuntimeError("provider down")
        return json.dumps(script["answer"])

    monkeypatch.setattr(gen.engine, "complete_chat", fake_llm)
    monkeypatch.setattr(gen.cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(gen.cache, "set", lambda *a, **k: None)

    st = gen.resume_state(owner, project)
    ok(not st["stale"] and st["superseded"] == 0, "existing cases backfill the basis: nothing pending")
    v1 = suites.publish(owner, project, owner)["version"]

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update brd_chunk set text = 'The customer can pay by card or wallet.' where id = %s", (pay_req,))
        cur.execute("delete from brd_chunk where id = %s", (refund_req,))
        cur.execute("insert into brd_chunk (document_id, req_id, section, ordinal, text, content_hash) values "
                    "(%s, 'FR-3', 'Payments', 2, 'The customer can download an invoice.', 'h3') returning id",
                    (sandbox["doc"],))
        invoice_req = cur.fetchone()[0]
        conn.commit()
    st = gen.resume_state(owner, project)
    ok(st["stale"] and (st["changed"], st["added"], st["removed"]) == (1, 1, 1),
       "status: 1 requirement changed, 1 added, 1 removed")

    # the model: update E1 (Pay by card), keep E2 (Card declined), add two new cases
    script["answer"] = {"existing": [{"ref": "E1", "action": "update",
                                      "use_case": _uc("Pay by card", [pay_req], ("open checkout", "choose card or wallet", "submit"))},
                                     {"ref": "E2", "action": "keep"}],
                        "new": [_uc("Pay with a wallet", [pay_req]), _uc("Download an invoice", [invoice_req])]}
    res = gen.generate_for_project(owner, project, mode="sync")
    ok(len(calls) == 1 and "EXISTING USE CASES" in calls[0] and "E2 [negative] Card declined" in calls[0],
       "ONE update call, given the existing cases by ref")
    ok((res["updated"], res["removed"], res["use_cases"], res["errors"]) == (1, 1, 2, []),
       "1 updated in place, 1 orphan removed (no LLM), 2 added")

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select uid, uc_id, title, steps from use_case where owner_id=%s and project=%s order by uid",
                    (owner, project))
        rows = {r[0]: r for r in cur.fetchall()}
        cur.execute("select change_kind from use_case_version where use_case_uid=%s order by version_no desc limit 1",
                    (refund["uid"],))
        refund_hist = cur.fetchone()[0]
    ok(pay["uid"] in rows and rows[pay["uid"]][1] == "UC-1" and "choose card or wallet" in rows[pay["uid"]][3],
       "the updated case kept its uid and code (UC-1)")
    ok(refund["uid"] not in rows and refund_hist == "regenerate", "the orphan is gone from the draft, kept in history")
    new_codes = sorted(r[1] for u, r in rows.items() if u not in (pay["uid"], declined["uid"]))
    ok(new_codes == ["UC-4", "UC-5"], "new cases continue the numbering — UC-3 is never reused")

    d = suites.diff(owner, project, v1["id"], "draft")
    by = {i["uid"]: i for i in d["items"]}
    ok(by[pay["uid"]]["change"] == "modified" and by[pay["uid"]]["retest"], "diff: UC-1 modified, needs retest")
    ok(by[declined["uid"]]["change"] == "requirement_changed" and by[declined["uid"]]["retest"],
       "diff: UC-2 unchanged text but its requirement changed → retest")
    ok(by[refund["uid"]]["change"] == "removed" and d["summary"]["added"] == 2, "diff: UC-3 removed, 2 added")
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from use_case_suite_member where suite_version_id=%s", (v1["id"],))
        ok(cur.fetchone()[0] == 3, "published v1 is untouched (still its 3 cases)")

    ok(not gen.resume_state(owner, project)["stale"], "after the update nothing is pending")
    calls.clear()
    res = gen.generate_for_project(owner, project, mode="sync")
    ok(not calls and res["batches"] == 0, "running again does nothing (no LLM call)")

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update brd_chunk set text = 'The customer can pay by card, wallet or voucher.' where id = %s",
                    (pay_req,))
        cur.execute("select md5(string_agg(uid::text || title || steps::text, ',' order by uid)) from use_case "
                    "where owner_id=%s and project=%s", (owner, project))
        before = cur.fetchone()[0]
        conn.commit()
    script["fail"] = True
    res = gen.generate_for_project(owner, project, mode="sync")
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select md5(string_agg(uid::text || title || steps::text, ',' order by uid)) from use_case "
                    "where owner_id=%s and project=%s", (owner, project))
        after = cur.fetchone()[0]
    ok(res["errors"] and after == before and gen.resume_state(owner, project)["changed"] == 1,
       "a failed unit changes nothing and stays pending")
    script["fail"] = False
    script["answer"] = {"existing": [], "new": []}                    # every case kept
    res = gen.generate_for_project(owner, project, mode="sync")
    ok(not res["errors"] and not gen.resume_state(owner, project)["stale"], "the next run completes it")

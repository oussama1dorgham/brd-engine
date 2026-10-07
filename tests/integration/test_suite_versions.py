"""Suite versions (migration 032): immutable, content-addressed snapshots of a BRD's use cases.

Runs on a throwaway BRD (its own document, requirements, folders, cases) and purges it after."""
import uuid

import psycopg
import pytest

from backend.core.db import pool
from backend.use_cases import suites, testing as uct

pytestmark = pytest.mark.integration


def ok(cond, msg):
    assert cond, msg
    print("PASS", msg)


def _members(cur, vid):
    cur.execute("select m.use_case_uid, m.content_id, m.folder_path, k.content_hash, k.def_hash, k.req_hashes "
                "from use_case_suite_member m join use_case_content k on k.id = m.content_id "
                "where m.suite_version_id = %s", (vid,))
    return {r[0]: {"content_id": r[1], "folder": r[2], "content_hash": r[3], "def_hash": r[4], "req": r[5]}
            for r in cur.fetchall()}


def test_suite_versions_lifecycle():
    project = f"zz-suite-{uuid.uuid4().hex[:8]}"
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select id from app_user order by id limit 1"); owner = cur.fetchone()[0]
        cur.execute("insert into brd_document (title, project, sha256, owner_id) values (%s, %s, 'x', %s) returning id",
                    (project, project, owner)); doc = cur.fetchone()[0]
        cur.execute("insert into brd_chunk (document_id, req_id, section, ordinal, text, content_hash) values "
                    "(%s, 'FR-1', 'S', 0, 'The user can log in.', 'h1'), "
                    "(%s, 'FR-2', 'S', 1, 'The user can log out.', 'h2') returning id", (doc, doc))
        r1, r2 = (r[0] for r in cur.fetchall())
        cur.execute("insert into use_case_folder (owner_id, project, name) values (%s, %s, 'Auth'), (%s, %s, 'Other') "
                    "returning id", (owner, project, owner, project))
        f1, f2 = (r[0] for r in cur.fetchall())
        cur.execute("insert into use_case (owner_id, project, folder_id, uc_id, title, steps, source_chunk_ids, case_type) "
                    "values (%s, %s, %s, 'UC-1', 'Log in', '[\"open\",\"submit\"]', %s, 'functional') returning uid",
                    (owner, project, f1, psycopg.types.json.Jsonb([r1]))); a = cur.fetchone()[0]
        cur.execute("insert into use_case (owner_id, project, folder_id, uc_id, title, steps, source_chunk_ids, case_type) "
                    "values (%s, %s, %s, 'UC-2', 'Bad password', '[\"wrong pw\"]', %s, 'negative') returning uid",
                    (owner, project, f1, psycopg.types.json.Jsonb([r2]))); b = cur.fetchone()[0]
        conn.commit()
    try:
        v1 = suites.snapshot(owner, project, owner, "baseline")
        ok(v1["created"] and v1["number"] == 1, "first snapshot creates v1")
        again = suites.snapshot(owner, project, owner)
        ok(not again["created"] and again["id"] == v1["id"], "publishing an unchanged draft is a no-op")

        with pool().connection() as conn, conn.cursor() as cur:
            m1 = _members(cur, v1["id"])
            ok(set(m1) == {a, b} and m1[a]["folder"] == ["Auth"], "v1 holds both cases with their folder path")
            ok(m1[a]["req"] == {str(r1): __import__("hashlib").md5(b"The user can log in.").hexdigest()},
               "v1 pins the cited requirement's text hash")
            cur.execute("update use_case set title = 'Log in (typo fixed)' where uid = %s", (a,)); conn.commit()
        v2 = suites.snapshot(owner, project, owner)
        with pool().connection() as conn, conn.cursor() as cur:
            m2 = _members(cur, v2["id"])
        ok(v2["number"] == 2, "an edit publishes v2")
        ok(m2[a]["content_hash"] != m1[a]["content_hash"] and m2[a]["def_hash"] == m1[a]["def_hash"],
           "title edit = modified, but test-relevant fields unchanged (no retest needed)")
        ok(m2[b]["content_id"] == m1[b]["content_id"], "an untouched case shares its content row across versions")

        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("update use_case set folder_id = %s where uid = %s", (f2, b)); conn.commit()
        v3 = suites.snapshot(owner, project, owner)
        with pool().connection() as conn, conn.cursor() as cur:
            m3 = _members(cur, v3["id"])
        ok(v3["number"] == 3 and m3[b]["content_id"] == m2[b]["content_id"] and m3[b]["folder"] == ["Other"],
           "a move is a new version with the SAME content (folder is placement, not content)")

        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("update brd_chunk set text = 'The user can log in with SSO.' where id = %s", (r1,)); conn.commit()
        v4 = suites.snapshot(owner, project, owner)
        with pool().connection() as conn, conn.cursor() as cur:
            m4 = _members(cur, v4["id"])
            cur.execute("select count(*) from use_case_req_text where owner_id = %s and project = %s", (owner, project))
            texts = cur.fetchone()[0]
        ok(m4[a]["content_hash"] == m3[a]["content_hash"] and m4[a]["content_id"] != m3[a]["content_id"],
           "requirement edited under an unchanged case → new content row (detected)")
        ok(texts == 3, "old AND new requirement text both kept (each version shows its own)")

        with pool().connection() as conn, conn.cursor() as cur:
            for sql, what in (("update use_case_content set data = '{}' where id = %s", "content"),
                              ("update use_case_suite_member set ordinal = 9 where content_id = %s", "member")):
                with pytest.raises(psycopg.errors.RaiseException):
                    cur.execute(sql, (m1[a]["content_id"],))
                conn.rollback()
                print("PASS", what, "rows are immutable")
            with pytest.raises(psycopg.errors.RaiseException):
                cur.execute("update use_case_suite_version set number = 99 where id = %s", (v1["id"],))
            conn.rollback()
            cur.execute("update use_case_suite_version set label = 'renamed' where id = %s", (v1["id"],)); conn.commit()
            print("PASS a version's number is frozen; its label may change")

            cur.execute("insert into test_cycle (owner_id, project, name, suite_version_id) values (%s, %s, 'R', %s)",
                        (owner, project, v1["id"])); conn.commit()
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                cur.execute("delete from use_case_suite_version where id = %s", (v1["id"],))
            conn.rollback()
            print("PASS a version with runs cannot be deleted")

        ok([v["number"] for v in suites.list_versions(owner, project)] == [4, 3, 2, 1], "versions listed newest first")
    finally:
        with pool().connection() as conn, conn.cursor() as cur:
            uct.purge_project(cur, owner, project)
            cur.execute("delete from use_case where owner_id = %s and project = %s", (owner, project))
            cur.execute("delete from use_case_folder where owner_id = %s and project = %s", (owner, project))
            cur.execute("delete from brd_document where project = %s", (project,))
            conn.commit()
            left = 0
            for t in ("use_case_suite_version", "use_case_content", "use_case_req_text", "test_cycle"):
                cur.execute(f"select count(*) from {t} where project = %s", (project,)); left += cur.fetchone()[0]
    ok(left == 0, "BRD purge removes versions, content, requirement texts and runs")


def test_publish_review_stale_and_audit():
    project = f"zz-suite-{uuid.uuid4().hex[:8]}"
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select id from app_user order by id limit 1"); owner = cur.fetchone()[0]
        cur.execute("insert into brd_document (title, project, sha256, owner_id) values (%s, %s, 'x', %s) returning id",
                    (project, project, owner)); doc = cur.fetchone()[0]
        cur.execute("insert into brd_chunk (document_id, req_id, section, ordinal, text, content_hash) "
                    "values (%s, 'FR-1', 'S', 0, 'Pay by card.', 'h') returning id", (doc,)); r1 = cur.fetchone()[0]
        uids = []
        for t in ("Pay", "Refund"):
            cur.execute("insert into use_case (owner_id, project, uc_id, title, steps, source_chunk_ids) "
                        "values (%s, %s, %s, %s, '[\"s\"]', %s) returning uid",
                        (owner, project, f"UC-{t}", t, psycopg.types.json.Jsonb([r1])))
            uids.append(cur.fetchone()[0])
        conn.commit()
    pay, refund = uids
    try:
        st = suites.status(owner, project)
        ok(st["latest"] is None and st["draft"]["added"] == 2, "no version yet: the whole draft is 'added'")
        v1 = suites.publish(owner, project, owner, "first", expect=st["fingerprint"])
        ok(v1["created"] and v1["version"]["number"] == 1 and v1["version"]["label"] == "first", "publish creates v1")
        ok(not suites.status(owner, project)["draft"]["has_changes"], "right after publish the draft is clean")

        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("update use_case set steps = '[\"s\",\"s2\"]' where uid = %s", (pay,))
            cur.execute("update brd_chunk set text = 'Pay by card or wallet.' where id = %s", (r1,))
            conn.commit()
        d = suites.diff(owner, project, v1["version"]["id"], "draft")
        by = {i["uid"]: i for i in d["items"]}
        ok(by[pay]["change"] == "modified" and by[pay]["retest"], "edited steps → modified, needs retest")
        ok(by[refund]["change"] == "requirement_changed"
           and by[refund]["requirements"][0]["before"] == "Pay by card."
           and by[refund]["requirements"][0]["after"] == "Pay by card or wallet.",
           "requirement edit under an untouched case shows old and new text")

        reviewed = d["fingerprint"]
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("update use_case set title = 'Refund (edited in another tab)' where uid = %s", (refund,))
            conn.commit()
        with pytest.raises(suites.Stale):
            suites.publish(owner, project, owner, expect=reviewed)
        print("PASS a draft changed after review cannot be published (stale)")

        fresh = suites.status(owner, project)["fingerprint"]
        v2 = suites.publish(owner, project, owner, expect=fresh)
        ok(v2["created"] and v2["version"]["number"] == 2 and v2["summary"]["retest"] == 2, "re-reviewed draft publishes v2")
        again = suites.publish(owner, project, owner)
        ok(not again["created"] and again["version"]["number"] == 2, "publishing an unchanged draft is a no-op")

        from backend.use_cases import audit as ua
        e = [x for x in ua.entries(owner, pay, "version")["entries"]]
        ok([x["summary"] for x in e] == ["Published in v2 — modified (steps)", "Published in v1 — added"]
           and all(x["suite_version"] in (1, 2) for x in e), "each publish audits the case with its version")
        ok(ua.entries(owner, refund, "version")["entries"][0]["detail"].get("retest") is True,
           "the audit says a requirement change requires a retest")
    finally:
        with pool().connection() as conn, conn.cursor() as cur:
            uct.purge_project(cur, owner, project)
            cur.execute("delete from use_case where owner_id = %s and project = %s", (owner, project))
            cur.execute("delete from brd_document where project = %s", (project,))
            conn.commit()

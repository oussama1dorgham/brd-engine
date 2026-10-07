"""Per-use-case AUDIT LOG — the complete, attributable, append-only history of a use case.

Every act on a use case — definition changes (create/edit/move/delete/restore/regenerate),
test verdicts and their amendments/undos, notes, evidence added/removed, scope changes, cycle
planning, and change proposals — appends ONE row: who did it (actor id + email, denormalized),
when, what (category/action + a human summary), the status before → after, the cycle, and a
detail payload. The table is append-only (a DB trigger rejects UPDATE); nothing in the app
deletes from it except purging a whole BRD. See migrations/030_use_case_audit.sql and
docs/USECASE_TESTING.md §14.

`log(cur, …)` runs in the CALLER's transaction, so an audit entry commits atomically with the
change it describes (a change can never happen without its trace, and vice versa).
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

from backend.core.db import pool

STATUS_LABEL = {"not_tested": "Not run", "in_progress": "In progress", "blocked": "Blocked",
                "failed": "Failed", "passed": "Passed"}
CATEGORIES = ("definition", "test", "evidence", "scope", "cycle", "proposal", "version")


def label(status: str | None) -> str:
    return STATUS_LABEL.get(status or "", status or "—")


def log(cur, *, uid: int, owner_id: int, project: str | None, actor_id: int | None, category: str,
        action: str, summary: str | None = None, status_before: str | None = None,
        status_after: str | None = None, cycle_id: int | None = None, version_no: int | None = None,
        result_id: int | None = None, detail: dict | None = None, source: str = "user",
        uc_id: str | None = None, suite_version_id: int | None = None) -> None:
    """Append one audit entry (caller's tx). uc_id / project / actor email / cycle name are
    resolved here so every entry is self-describing even after the card or user is gone."""
    detail = {k: v for k, v in (detail or {}).items() if v not in (None, "", [], {})} or None
    cur.execute(
        "insert into use_case_audit (use_case_uid, owner_id, project, uc_id, actor_id, actor_email, source, "
        "category, action, summary, status_before, status_after, cycle_id, cycle_name, version_no, result_id, detail, "
        "suite_version_id) "
        "values (%s, %s, coalesce(%s, (select project from use_case where uid = %s limit 1), ''), "
        "coalesce(%s, (select uc_id from use_case where uid = %s limit 1)), %s, "
        "(select email from app_user where id = %s), %s, %s, %s, %s, %s, %s, %s, "
        "(select name from test_cycle where id = %s), %s, %s, %s, %s)",
        (uid, owner_id, project, uid, uc_id, uid, actor_id, actor_id, source, category, action, summary,
         status_before, status_after, cycle_id, cycle_id, version_no, result_id,
         Jsonb(detail) if detail else None, suite_version_id),
    )


def entries(owner_id: int, uid: int, category: str | None = None, limit: int = 500) -> dict:
    """The audit log of one use case, newest first (optionally one category)."""
    where = "where use_case_uid = %s and owner_id = %s"
    params: list = [uid, owner_id]
    if category in CATEGORIES:
        where += " and category = %s"
        params.append(category)
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "select id, created_at, actor_email, source, category, action, summary, status_before, status_after, "
            "cycle_id, cycle_name, version_no, result_id, detail, uc_id, "
            "(select number from use_case_suite_version v where v.id = suite_version_id) from use_case_audit "
            f"{where} order by created_at desc, id desc limit %s", params + [max(1, min(limit, 2000))])
        rows = cur.fetchall()
        cur.execute("select category, count(*) from use_case_audit where use_case_uid = %s and owner_id = %s "
                    "group by category", (uid, owner_id))
        counts = dict(cur.fetchall())
    return {"uid": uid, "counts": counts, "entries": [{
        "id": r[0], "at": r[1].isoformat(), "actor": r[2], "source": r[3], "category": r[4], "action": r[5],
        "summary": r[6], "status_before": r[7], "status_after": r[8], "cycle_id": r[9], "cycle_name": r[10],
        "version_no": r[11], "result_id": r[12], "detail": r[13] or {}, "uc_id": r[14], "suite_version": r[15],
    } for r in rows]}


def purge_project(cur, owner_id: int, project: str) -> None:
    """BRD deletion only — the one sanctioned removal of audit rows."""
    cur.execute("delete from use_case_audit where owner_id = %s and project = %s", (owner_id, project))

"""Integration tests run against the LOCAL stack: Postgres (brd_real) — and the ingestion
lifecycle also real Voyage + the object store. Each test cleans up everything it creates,
but they spend free-tier Voyage calls, so they are collected only on request:

    RUN_INTEGRATION=1 .venv/Scripts/python.exe -m pytest tests/integration -s

The default `pytest` run (unit tests only) stays offline and fast.

Tests that act on use cases use the `sandbox` fixture — a throwaway BRD of their own — so they
never touch real BRDs (whose use-case history is now versioned: a stray edit there would show
up as an unpublished change, or worse, be published).
"""
import os
import uuid

import pytest

collect_ignore_glob = [] if os.getenv("RUN_INTEGRATION") == "1" else ["test_*.py"]

_TABLES = ("use_case", "use_case_folder", "use_case_version", "use_case_test_result", "use_case_test_proof",
           "use_case_audit", "test_cycle", "use_case_suite_version", "use_case_content", "use_case_req_text",
           "use_case_change_proposal", "use_case_req_basis", "brd_document")


@pytest.fixture(autouse=True)
def _no_upload_quota_leak():
    """Uploads a test makes count toward the owner's REAL hourly upload limit
    (ingest_upload_log deliberately survives BRD deletes, migration 037) — and the sandbox
    owner is a real account. Remove exactly the log rows this test created."""
    from backend.core.db import pool
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select coalesce(max(id), 0) from ingest_upload_log")
        start = cur.fetchone()[0]
    yield
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("delete from ingest_upload_log where id > %s", (start,))
        conn.commit()


@pytest.fixture
def sandbox():
    """A throwaway BRD: 2 requirements, a scope folder with a sub-folder, and 3 not-yet-run use
    cases citing them, published as v1 (like every real BRD since migration 032). Torn down
    through the app's own BRD-delete purges, then checked empty."""
    from psycopg.types.json import Jsonb
    from backend.chat.history_store import delete_project
    from backend.core.db import pool
    from backend.use_cases import generation, suites, versioning

    project = f"zz-it-{uuid.uuid4().hex[:8]}"
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select id from app_user order by id limit 1")
        owner = cur.fetchone()[0]
        cur.execute("insert into brd_document (title, project, sha256, owner_id) values (%s, %s, 'x', %s) returning id",
                    (project, project, owner))
        doc = cur.fetchone()[0]
        cur.execute("insert into brd_chunk (document_id, req_id, section, ordinal, text, content_hash) values "
                    "(%s, 'FR-1', 'Payments', 0, 'The customer can pay by card.', 'h1'), "
                    "(%s, 'FR-2', 'Payments', 1, 'The customer can request a refund.', 'h2') returning id", (doc, doc))
        chunks = [r[0] for r in cur.fetchall()]
        cur.execute("insert into use_case_folder (owner_id, project, name) values (%s, %s, 'Payments') returning id",
                    (owner, project))
        scope = cur.fetchone()[0]
        cur.execute("insert into use_case_folder (owner_id, project, parent_id, name) values (%s, %s, %s, 'Cards') "
                    "returning id", (owner, project, scope))
        sub = cur.fetchone()[0]
        cases = []
        for i, (title, cid, ctype) in enumerate((("Pay by card", chunks[0], "functional"),
                                                 ("Card declined", chunks[0], "negative"),
                                                 ("Request a refund", chunks[1], "functional"))):
            cur.execute("insert into use_case (owner_id, project, folder_id, uc_id, title, description, roles, "
                        "preconditions, steps, expected_behaviour, source_chunk_ids, ordinal, case_type, batch_hash) "
                        "values (%s, %s, %s, %s, %s, '', %s, '', %s, 'done', %s, %s, %s, 'zz-batch') "
                        "returning id, uid",
                        (owner, project, sub, f"UC-{i + 1}", title, Jsonb(["Customer"]),
                         Jsonb(["open checkout", "submit"]), Jsonb([cid]), i, ctype))
            pk, uid = cur.fetchone()
            versioning.record_change(cur, pk, "create", owner, owner)    # as generation does: history v1
            cases.append({"id": pk, "uid": uid, "title": title})
        conn.commit()
    v1 = suites.snapshot(owner, project, owner, "baseline", "Baseline")
    generation.resume_state(owner, project)          # establishes the requirement basis, as a first load does

    yield {"owner": owner, "project": project, "doc": doc, "chunks": chunks, "scope_folder": scope,
           "folder": sub, "cases": cases, "v1": v1["id"]}

    delete_project(project, owner)
    generation.purge_project(owner, project)
    with pool().connection() as conn, conn.cursor() as cur:
        left = {}
        for t in _TABLES:
            cur.execute(f"select count(*) from {t} where project = %s", (project,))
            left[t] = cur.fetchone()[0]
    assert not any(left.values()), f"BRD delete left rows behind: { {t: n for t, n in left.items() if n} }"

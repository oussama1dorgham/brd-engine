"""Integration tests for the revision-safety fixes in backend/ingest/revise.py:

  - a requirement added to the live BRD after a compare must make the revision stale
    (apply refuses, get_open reports it) — the old check only covered ids the revision's
    items referenced, missing one added/revived after the fact.
  - applying a revision that would archive almost the whole live BRD needs an explicit
    confirm_replace=True (NeedsConfirmation otherwise).
  - compare() superseding an older open revision must also stop that revision's ingest_job
    from being left in 'review' forever.

Uses the `sandbox` fixture (a throwaway BRD, 2 requirements) — no Voyage calls; revisions
are built with revise.compare() directly against hand-written new_rows, so no embeddings
are needed for the paths exercised here (same / removed items need no vector; this suite
never applies a modified/added item, so nothing needs a fake vector either).
"""
import hashlib

import pytest

from backend.core.db import pool
from backend.ingest import revise

pytestmark = pytest.mark.integration


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _row(text: str, req_id: str, section: str = "Payments") -> dict:
    return {"text": text, "content_hash": _hash(text), "req_id": req_id, "section": section,
            "section_path": None, "page": None, "token_count": len(text.split()), "embed_hash": None}


def _add_live_chunk(cur, document_id: int, req_id: str, ordinal: int, text: str) -> int:
    cur.execute(
        "insert into brd_chunk (document_id, req_id, section, ordinal, text, content_hash) "
        "values (%s, %s, 'Payments', %s, %s, %s) returning id",
        (document_id, req_id, ordinal, text, _hash(text)),
    )
    return cur.fetchone()[0]


def _fix_sandbox_hashes(cur, document_id: int) -> None:
    """The sandbox fixture seeds FR-1/FR-2 with placeholder content_hash values ('h1'/'h2' —
    fine for the use-case tests it was built for, which never align by hash). Alignment's
    exact-content-hash match needs the REAL hash of the stored text, so recompute it here."""
    cur.execute("select id, text from brd_chunk where document_id = %s", (document_id,))
    for chunk_id, text in cur.fetchall():
        cur.execute("update brd_chunk set content_hash = %s where id = %s", (_hash(text), chunk_id))


def test_requirement_added_after_compare_makes_the_revision_stale(sandbox):
    owner, project, doc = sandbox["owner"], sandbox["project"], sandbox["doc"]

    # FR-1 unchanged (same), FR-2 edited (modified) — a normal, small revision
    new_rows = [_row("The customer can pay by card.", "FR-1"),
                _row("The customer can request a refund within 30 days.", "FR-2")]
    with pool().connection() as conn, conn.cursor() as cur:
        _fix_sandbox_hashes(cur, doc)
        staged = revise.compare(cur, owner_id=owner, document_id=doc, project=project,
                                 new_rows=new_rows, job_id=None, source_id=None)
        conn.commit()
    revision_id = staged["revision_id"]
    assert staged["summary"] == {"same": 1, "modified": 1, "added": 0, "removed": 0}

    opened = revise.get_open(owner, project)
    assert opened["id"] == revision_id and opened["stale"] is False

    # a requirement is added to the live BRD AFTER the compare (e.g. an edit, or a revive) —
    # the live id set no longer matches exactly what the revision's items reference
    with pool().connection() as conn, conn.cursor() as cur:
        _add_live_chunk(cur, doc, "FR-3", 2, "The customer can view their payment history.")
        conn.commit()

    opened = revise.get_open(owner, project)
    assert opened["id"] == revision_id
    assert opened["stale"] is True, "an id added after the compare must flag the revision stale"

    with pytest.raises(revise.Stale):
        revise.apply_revision(owner, revision_id, by=owner)

    revise.discard_revision(owner, revision_id, by=owner)


def test_near_total_replacement_needs_confirmation(sandbox):
    owner, project, doc = sandbox["owner"], sandbox["project"], sandbox["doc"]

    # bring live to 6 requirements (FR-1, FR-2 from the fixture + 4 more)
    with pool().connection() as conn, conn.cursor() as cur:
        _fix_sandbox_hashes(cur, doc)
        for i, req in enumerate(("FR-3", "FR-4", "FR-5", "FR-6"), start=2):
            _add_live_chunk(cur, doc, req, i, f"Requirement {req} body text, distinct from the others.")
        conn.commit()

    # the "new file" matches only FR-1 — the other 5 would be archived (matched 1/6 < 20%)
    new_rows = [_row("The customer can pay by card.", "FR-1")]
    with pool().connection() as conn, conn.cursor() as cur:
        staged = revise.compare(cur, owner_id=owner, document_id=doc, project=project,
                                 new_rows=new_rows, job_id=None, source_id=None)
        conn.commit()
    revision_id = staged["revision_id"]
    assert staged["summary"] == {"same": 1, "modified": 0, "added": 0, "removed": 5}

    opened = revise.get_open(owner, project)
    assert opened["needs_confirmation"] is True
    assert opened["replace_ratio"] < 0.2

    with pytest.raises(revise.NeedsConfirmation) as exc:
        revise.apply_revision(owner, revision_id, by=owner)
    summary = exc.value.summary
    assert summary["live"] == 6 and summary["matched"] == 1 and summary["removed"] == 5
    assert summary["matched_ratio"] < 0.2

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select review_status from brd_document where id = %s", (doc,))
        assert cur.fetchone()[0] == "approved"          # unchanged — the refused apply did nothing

    result = revise.apply_revision(owner, revision_id, by=owner, confirm_replace=True)
    assert result["kept"] == 1 and result["archived"] == 5

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from brd_chunk where document_id = %s", (doc,))
        assert cur.fetchone()[0] == 1                    # only FR-1 remains live
        cur.execute("select count(*) from brd_chunk_archive where document_id = %s", (doc,))
        assert cur.fetchone()[0] == 5
        cur.execute("select review_status from brd_document where id = %s", (doc,))
        assert cur.fetchone()[0] == "draft", "applying a revision to an approved BRD must flip it to draft"


def test_skipped_modifications_count_as_matched_not_near_total_replacement(sandbox):
    """Regression: the replace-ratio gate used to count only APPLIED modifications as
    'matched', so skipping every modification (keeping the current text) made a revision
    look like it replaces almost the whole BRD even though every live row stays exactly as
    it is. matched must be same + ALL modified (applied or skipped) + kept removed."""
    owner, project, doc = sandbox["owner"], sandbox["project"], sandbox["doc"]

    # bring live to 10 requirements (FR-1, FR-2 from the fixture + 8 more)
    with pool().connection() as conn, conn.cursor() as cur:
        _fix_sandbox_hashes(cur, doc)
        for i, req in enumerate(("FR-3", "FR-4", "FR-5", "FR-6", "FR-7", "FR-8", "FR-9", "FR-10"), start=2):
            _add_live_chunk(cur, doc, req, i, f"Requirement {req} body text, distinct from the others.")
        conn.commit()
        cur.execute("select text from brd_chunk where document_id = %s and req_id = 'FR-2'", (doc,))
        fr2_original_text = cur.fetchone()[0]

    # FR-1 unchanged (same); FR-2..FR-9 each get a small wording change (modified); FR-10
    # dropped (removed). 1 same + 8 modified + 1 removed, live = 10.
    new_rows = [_row("The customer can pay by card.", "FR-1")]
    new_rows.append(_row(fr2_original_text + " Updated.", "FR-2"))
    for req in ("FR-3", "FR-4", "FR-5", "FR-6", "FR-7", "FR-8", "FR-9"):
        new_rows.append(_row(f"Requirement {req} body text, distinct from the others. Updated.", req))
    with pool().connection() as conn, conn.cursor() as cur:
        staged = revise.compare(cur, owner_id=owner, document_id=doc, project=project,
                                 new_rows=new_rows, job_id=None, source_id=None)
        conn.commit()
    revision_id = staged["revision_id"]
    assert staged["summary"] == {"same": 1, "modified": 8, "added": 0, "removed": 1}

    opened = revise.get_open(owner, project)
    skip_ids = [it["id"] for it in opened["items"] if it["change"] == "modified"]
    assert len(skip_ids) == 8

    # skip every modification (keep current text) and don't keep the removed one —
    # matched must be 1 (same) + 8 (modified, skipped but still matched) = 9 of 10 (90%),
    # well above the confirmation threshold — no NeedsConfirmation.
    result = revise.apply_revision(owner, revision_id, by=owner, skip=skip_ids)
    assert result["kept"] == 9        # FR-1 (same) + 8 skipped modifications, unchanged in place
    assert result["archived"] == 1    # FR-10, removed and not kept

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from brd_chunk where document_id = %s", (doc,))
        assert cur.fetchone()[0] == 9
        cur.execute("select text from brd_chunk where document_id = %s and req_id = 'FR-2'", (doc,))
        assert cur.fetchone()[0] == fr2_original_text  # skipped: text not updated


def test_compare_fence_raising_writes_nothing(sandbox):
    """Contract for the jobs runner (backend/ingest/jobs.py): `fence` runs inside compare()'s
    transaction right after the document is locked and BEFORE any write; if it raises, the
    exception propagates and nothing from this compare() is persisted — no revision row, no
    superseding of an older one."""
    owner, project, doc = sandbox["owner"], sandbox["project"], sandbox["doc"]
    with pool().connection() as conn, conn.cursor() as cur:
        _fix_sandbox_hashes(cur, doc)
        conn.commit()

    class _LeaseLost(Exception):
        pass

    def _fence(_cur):
        raise _LeaseLost("job lost its lease")

    with pool().connection() as conn, conn.cursor() as cur:
        with pytest.raises(_LeaseLost):
            revise.compare(cur, owner_id=owner, document_id=doc, project=project,
                            new_rows=[_row("The customer can pay by card.", "FR-1")],
                            job_id=None, source_id=None, fence=_fence)

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from brd_revision where document_id = %s", (doc,))
        assert cur.fetchone()[0] == 0


def test_superseded_revision_job_is_canceled_not_left_in_review(sandbox):
    owner, project, doc = sandbox["owner"], sandbox["project"], sandbox["doc"]

    with pool().connection() as conn, conn.cursor() as cur:
        _fix_sandbox_hashes(cur, doc)
        cur.execute(
            "insert into ingest_job (owner_id, project, document_id, mode, status) "
            "values (%s, %s, %s, 'revision', 'review') returning id",
            (owner, project, doc))
        job_id = cur.fetchone()[0]
        first = revise.compare(cur, owner_id=owner, document_id=doc, project=project,
                                new_rows=[_row("The customer can pay by card.", "FR-1")],
                                job_id=job_id, source_id=None)
        conn.commit()

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select status, error from ingest_job where id = %s", (job_id,))
        status, error = cur.fetchone()
        assert (status, error) == ("review", None)       # not yet superseded

        second = revise.compare(cur, owner_id=owner, document_id=doc, project=project,
                                 new_rows=[_row("The customer can pay by card or cash.", "FR-1")],
                                 job_id=None, source_id=None)
        conn.commit()
    assert second["revision_id"] != first["revision_id"]

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select status from brd_revision where id = %s", (first["revision_id"],))
        assert cur.fetchone()[0] == "superseded"
        cur.execute("select status, error from ingest_job where id = %s", (job_id,))
        status, error = cur.fetchone()
        assert status == "canceled", "a superseded revision's job must not be left in 'review' forever"
        assert error == "Superseded by a newer version"

    revise.discard_revision(owner, second["revision_id"], by=owner)

"""BRD document versioning + approval — the whole-BRD safety net.

A snapshot is an immutable copy of a document's full chunk set (text + embedding),
so restore is exact and needs no re-embedding. Editing a requirement flips the BRD
to 'draft' (capturing an 'approved' baseline on the first edit of a cycle); a
reviewer then Approves (locks a version) or Discards (restores the baseline).

Note (scope): edits apply live and are rolled back on Discard — an optimistic
apply + review, not a hard gate that hides drafts from retrieval.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

from . import cache
from .db import pool
from .ingest.chunker import _est_tokens, _hash


class DocNotFound(Exception):
    """No BRD for this owner/project."""


def _resolve_doc(cur, owner_id: int, project: str) -> int | None:
    cur.execute("select id from brd_document where owner_id = %s and project = %s order by id desc limit 1",
                (owner_id, project))
    r = cur.fetchone()
    return r[0] if r else None


def _capture(cur, document_id: int) -> list[dict]:
    """Full ordered chunk set with embeddings (as pgvector text literals)."""
    cur.execute(
        """select c.req_id, c.section, c.ordinal, c.text, e.model, e.embedding::text
           from brd_chunk c left join brd_embedding e on e.chunk_id = c.id
           where c.document_id = %s order by c.ordinal""",
        (document_id,),
    )
    return [{"req_id": r[0], "section": r[1], "ordinal": r[2], "text": r[3], "model": r[4], "embedding": r[5]}
            for r in cur.fetchall()]


def _snapshot(cur, document_id: int, kind: str, label: str | None, created_by: int | None) -> int:
    cur.execute(
        "insert into brd_snapshot (document_id, label, kind, created_by, data) values (%s, %s, %s, %s, %s) returning id",
        (document_id, label, kind, created_by, Jsonb(_capture(cur, document_id))),
    )
    return cur.fetchone()[0]


def _restore_data(cur, document_id: int, data: list[dict]) -> None:
    """Replace the document's chunks with a snapshot's contents (exact)."""
    cur.execute("delete from brd_chunk where document_id = %s", (document_id,))   # cascades embeddings
    for row in data:
        cur.execute(
            """insert into brd_chunk (document_id, req_id, section, ordinal, text, token_count, content_hash)
               values (%s, %s, %s, %s, %s, %s, %s) returning id""",
            (document_id, row.get("req_id"), row.get("section"), row["ordinal"], row["text"],
             _est_tokens(row["text"]), _hash(row["text"])),
        )
        cid = cur.fetchone()[0]
        if row.get("embedding") and row.get("model"):
            cur.execute("insert into brd_embedding (chunk_id, model, embedding) values (%s, %s, %s::vector)",
                        (cid, row["model"], row["embedding"]))


# --- called from the edit path ---------------------------------------------

def ensure_baseline(document_id: int, created_by: int | None = None) -> None:
    """Before the first edit of a review cycle: snapshot the approved state as a
    restorable baseline and flip the doc to 'draft'. No-op if already 'draft'."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select review_status from brd_document where id = %s", (document_id,))
        row = cur.fetchone()
        if not row or row[0] != "approved":
            return
        _snapshot(cur, document_id, "approved", "Baseline (before edits)", created_by)
        cur.execute("update brd_document set review_status = 'draft' where id = %s", (document_id,))
        conn.commit()


# --- public API -------------------------------------------------------------

def status(owner_id: int, project: str) -> dict:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select id, review_status from brd_document where owner_id = %s and project = %s "
                    "order by id desc limit 1", (owner_id, project))
        r = cur.fetchone()
    if not r:
        raise DocNotFound(project)
    return {"document_id": r[0], "review_status": r[1]}


def list_versions(owner_id: int, project: str) -> dict:
    with pool().connection() as conn, conn.cursor() as cur:
        doc = _resolve_doc(cur, owner_id, project)
        if not doc:
            raise DocNotFound(project)
        cur.execute("select review_status from brd_document where id = %s", (doc,))
        review_status = cur.fetchone()[0]
        cur.execute(
            """select s.id, s.label, s.kind, s.created_at, u.email, jsonb_array_length(s.data)
               from brd_snapshot s left join app_user u on u.id = s.created_by
               where s.document_id = %s order by s.created_at desc""",
            (doc,),
        )
        versions = [{"id": r[0], "label": r[1], "kind": r[2], "created_at": r[3].isoformat(),
                     "created_by": r[4], "requirements": r[5]} for r in cur.fetchall()]
    return {"review_status": review_status, "versions": versions}


def snapshot(owner_id: int, project: str, label: str | None = None, created_by: int | None = None) -> dict:
    with pool().connection() as conn, conn.cursor() as cur:
        doc = _resolve_doc(cur, owner_id, project)
        if not doc:
            raise DocNotFound(project)
        sid = _snapshot(cur, doc, "manual", (label or "").strip() or None, created_by)
        conn.commit()
    return {"snapshot_id": sid}


def restore(owner_id: int, project: str, snapshot_id: int, created_by: int | None = None) -> dict:
    with pool().connection() as conn, conn.cursor() as cur:
        doc = _resolve_doc(cur, owner_id, project)
        if not doc:
            raise DocNotFound(project)
        cur.execute("select data from brd_snapshot where id = %s and document_id = %s", (snapshot_id, doc))
        row = cur.fetchone()
        if not row:
            raise DocNotFound(f"snapshot {snapshot_id}")
        _snapshot(cur, doc, "auto", "Before restore", created_by)   # make the restore itself undoable
        _restore_data(cur, doc, row[0])
        cur.execute("update brd_document set review_status = 'approved', updated_at = now() where id = %s", (doc,))
        conn.commit()
    cache.bust_project(project)
    return {"restored": True, "snapshot_id": snapshot_id}


def approve(owner_id: int, project: str, label: str | None = None, created_by: int | None = None) -> dict:
    """Lock in the current state as an approved version; clears the draft flag."""
    with pool().connection() as conn, conn.cursor() as cur:
        doc = _resolve_doc(cur, owner_id, project)
        if not doc:
            raise DocNotFound(project)
        sid = _snapshot(cur, doc, "approved", (label or "").strip() or "Approved", created_by)
        cur.execute("update brd_document set review_status = 'approved' where id = %s", (doc,))
        conn.commit()
    return {"approved": True, "snapshot_id": sid}


def discard(owner_id: int, project: str, created_by: int | None = None) -> dict:
    """Reject the draft edits: restore the most recent approved snapshot."""
    with pool().connection() as conn, conn.cursor() as cur:
        doc = _resolve_doc(cur, owner_id, project)
        if not doc:
            raise DocNotFound(project)
        cur.execute("select id from brd_snapshot where document_id = %s and kind = 'approved' "
                    "order by created_at desc limit 1", (doc,))
        r = cur.fetchone()
    if not r:
        return {"discarded": False, "reason": "no approved version to restore"}
    restore(owner_id, project, r[0], created_by)
    return {"discarded": True, "snapshot_id": r[0]}

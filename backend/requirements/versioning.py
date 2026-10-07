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

from backend.core import cache
from backend.core.db import pool
from backend.ingest.chunker import _est_tokens, _hash


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
        """select c.req_id, c.section, c.ordinal, c.text, e.model, e.embedding::text, c.id,
                  c.section_path, c.page, c.embed_hash
           from brd_chunk c left join brd_embedding e on e.chunk_id = c.id
           where c.document_id = %s order by c.ordinal""",
        (document_id,),
    )
    return [{"req_id": r[0], "section": r[1], "ordinal": r[2], "text": r[3], "model": r[4], "embedding": r[5],
             "id": r[6], "section_path": r[7], "page": r[8], "embed_hash": r[9]}
            for r in cur.fetchall()]


def _snapshot(cur, document_id: int, kind: str, label: str | None, created_by: int | None) -> int:
    cur.execute(
        "insert into brd_snapshot (document_id, label, kind, created_by, data) values (%s, %s, %s, %s, %s) returning id",
        (document_id, label, kind, created_by, Jsonb(_capture(cur, document_id))),
    )
    return cur.fetchone()[0]


def _restore_data(cur, document_id: int, data: list[dict], created_by: int | None = None) -> dict:
    """Make the document's requirements equal a snapshot — ID-PRESERVING (ingest.revise.reconcile).

    Each snapshot row is matched to a live requirement (same id first, then same content /
    requirement id / similar text) and updated in place, or to an ARCHIVED one (same id or
    content) which is revived under its original id; only genuinely new rows are inserted.
    Live requirements the snapshot doesn't contain are archived, never deleted — so use-case
    citations, chat sources and the requirement audit keep resolving across a restore. The
    snapshot's stored vectors are reused (no re-embedding)."""
    from backend.ingest import revise

    live = revise._live(cur, document_id)
    live_ids = {r["id"] for r in live}
    cur.execute("select id, content_hash from brd_chunk_archive where document_id = %s", (document_id,))
    archived = dict(cur.fetchall())
    archived_by_hash: dict[str, list[int]] = {}
    for aid, h in archived.items():
        archived_by_hash.setdefault(h, []).append(aid)
    rows = [{**r, "content_hash": _hash(r["text"])} for r in data]
    pairs = revise.align(live, rows)
    used_live: set[int] = set()
    used_arch: set[int] = set()
    targets: list = []
    for j, r in enumerate(rows):
        t = revise.Target(text=r["text"], content_hash=r["content_hash"], req_id=r.get("req_id"),
                          section=r.get("section"), section_path=r.get("section_path"), page=r.get("page"),
                          token_count=_est_tokens(r["text"]), embed_hash=r.get("embed_hash"))
        rid = r.get("id")
        if rid in live_ids and rid not in used_live:
            t.live_id = rid
        elif j in pairs and live[pairs[j][0]]["id"] not in used_live:
            t.live_id = live[pairs[j][0]]["id"]
        elif rid in archived and rid not in used_arch:
            t.archived_id = rid
        else:
            aid = next((a for a in archived_by_hash.get(r["content_hash"], []) if a not in used_arch), None)
            t.archived_id = aid
        if t.live_id is not None:
            used_live.add(t.live_id)
            same = next(x for x in live if x["id"] == t.live_id)["content_hash"] == r["content_hash"]
            if not same and r.get("embedding"):
                t.vec = r["embedding"]
        elif t.archived_id is not None:
            used_arch.add(t.archived_id)
        else:
            t.vec = r.get("embedding")
        targets.append(t)
    return revise.reconcile(cur, document_id, targets, reason="restore", by=created_by,
                            model=next((r.get("model") for r in data if r.get("model")), None))


# --- called from the edit path ---------------------------------------------

def ensure_baseline_tx(cur, document_id: int, created_by: int | None = None) -> None:
    """Cursor-level baseline: snapshot the approved state + flip to 'draft', inside
    the CALLER's transaction (no commit). Lets an atomic edit capture its baseline
    and its writes in one tx, so a crash can't leave a draft flag with no edit.
    No-op unless the doc is currently 'approved'."""
    cur.execute("select review_status from brd_document where id = %s", (document_id,))
    row = cur.fetchone()
    if not row or row[0] != "approved":
        return
    _snapshot(cur, document_id, "approved", "Baseline (before edits)", created_by)
    cur.execute("update brd_document set review_status = 'draft' where id = %s", (document_id,))


def ensure_baseline(document_id: int, created_by: int | None = None) -> None:
    """Before the first edit of a review cycle: snapshot the approved state as a
    restorable baseline and flip the doc to 'draft'. No-op if already 'draft'."""
    with pool().connection() as conn, conn.cursor() as cur:
        ensure_baseline_tx(cur, document_id, created_by)
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
    from backend.ingest import revise

    with pool().connection() as conn, conn.cursor() as cur:
        doc = _resolve_doc(cur, owner_id, project)
        if not doc:
            raise DocNotFound(project)
        # Same lock order revise.py's revision paths use (advisory lock on document_id, THEN
        # the document row FOR UPDATE), taken BEFORE any chunk read/write below — otherwise
        # this path and a revision apply/recompare/discard could lock the document row and a
        # chunk row in opposite orders and deadlock.
        revise._lock_doc(cur, owner_id, doc)
        cur.execute("select data from brd_snapshot where id = %s and document_id = %s", (snapshot_id, doc))
        row = cur.fetchone()
        if not row:
            raise DocNotFound(f"snapshot {snapshot_id}")
        _snapshot(cur, doc, "auto", "Before restore", created_by)   # make the restore itself undoable
        _restore_data(cur, doc, row[0], created_by)
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

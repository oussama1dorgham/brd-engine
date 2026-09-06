"""In-place requirement (chunk) editing — QA "change a requirement".

Phase 1: edit one requirement's text → re-embed → re-index → cache-bust → audit.
Phase 2: structural edits — an over-long edit SPLITS into several chunks, and
requirements can be ADDED or REMOVED. Inserting shifts ordinals, so the
(document_id, ordinal) uniqueness is DEFERRABLE (migration 015) and we defer it
for the shift; uniqueness is validated at commit.

Invariants every mutating op keeps:
  * embed OUTSIDE the write tx (slow network call — never hold a row lock over it),
  * chunk text + embedding written in ONE tx (they never disagree),
  * `search_tsv` (generated) recomputes automatically; HNSW updates on embedding write,
  * the project's retrieve+answer caches are busted (answer cache is keyed on chunk
    ids, and a chunk's text can change under a stable id, so it MUST be busted),
  * an audit row records the change (kind edit|add|delete, old → new, who, when).
"""
from __future__ import annotations

from .. import cache
from ..config import settings
from ..db import pool
from ..versioning import ensure_baseline
from .chunker import _est_tokens, _hash, _split
from .embedder import embed_documents
from .store import _vec_literal


class ChunkNotFound(Exception):
    """No such chunk/BRD for this owner (missing, or not owned by the user)."""


def _defer_ordinals(cur) -> None:
    """Defer the (document_id, ordinal) uniqueness so we can shift within the tx."""
    cur.execute("set constraints brd_chunk_doc_ordinal_uk deferred")


def _insert_chunk(cur, document_id: int, req_id, section, ordinal: int,
                  text: str, vec_lit: str) -> int:
    cur.execute(
        """insert into brd_chunk (document_id, req_id, section, ordinal, text, token_count, content_hash)
           values (%s, %s, %s, %s, %s, %s, %s) returning id""",
        (document_id, req_id, section, ordinal, text, _est_tokens(text), _hash(text)),
    )
    cid = cur.fetchone()[0]
    cur.execute(
        "insert into brd_embedding (chunk_id, model, embedding) values (%s, %s, %s::vector)",
        (cid, settings.embed_model_docs, vec_lit),
    )
    return cid


def _audit(cur, document_id: int, chunk_id, req_id, old_text, new_text, kind, changed_by) -> None:
    cur.execute(
        """insert into requirement_change
             (document_id, chunk_id, req_id, old_text, new_text, kind, changed_by)
           values (%s, %s, %s, %s, %s, %s, %s)""",
        (document_id, chunk_id, req_id, old_text, new_text, kind, changed_by),
    )
    cur.execute("update brd_document set updated_at = now() where id = %s", (document_id,))


# --- reads ------------------------------------------------------------------

def list_requirements(owner_id: int, project: str) -> list[dict]:
    """Editable requirements (chunks) for one of the owner's BRDs, in document order."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """select c.id, c.req_id, c.section, c.ordinal, c.text
               from brd_chunk c join brd_document d on d.id = c.document_id
               where d.owner_id = %s and d.project = %s
               order by c.ordinal""",
            (owner_id, project),
        )
        return [{"chunk_id": r[0], "req_id": r[1], "section": r[2], "ordinal": r[3], "text": r[4]}
                for r in cur.fetchall()]


def requirement_history(owner_id: int, chunk_id: int) -> list[dict]:
    """Audit trail for one requirement (owner-scoped), newest first."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """select rc.id, rc.kind, rc.old_text, rc.new_text, rc.changed_at, u.email
               from requirement_change rc
               join brd_document d on d.id = rc.document_id
               left join app_user u on u.id = rc.changed_by
               where rc.chunk_id = %s and d.owner_id = %s
               order by rc.changed_at desc""",
            (chunk_id, owner_id),
        )
        return [{"id": r[0], "kind": r[1], "old_text": r[2], "new_text": r[3],
                 "changed_at": r[4].isoformat(), "changed_by": r[5]} for r in cur.fetchall()]


# --- mutations --------------------------------------------------------------

def update_requirement(owner_id: int, chunk_id: int, new_text: str,
                       changed_by: int | None = None, kind: str = "edit") -> dict:
    """Replace a requirement's text. If the new text exceeds the chunk ceiling it is
    SPLIT: the first piece stays in this chunk (keeps its id/history), the rest are
    inserted right after. Returns {chunk_id, req_id, project, changed, chunks}."""
    new_text = (new_text or "").strip()
    if not new_text:
        raise ValueError("new text is empty")

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """select c.text, c.req_id, c.section, c.ordinal, c.document_id, d.project
               from brd_chunk c join brd_document d on d.id = c.document_id
               where c.id = %s and d.owner_id = %s""",
            (chunk_id, owner_id),
        )
        row = cur.fetchone()
    if not row:
        raise ChunkNotFound(chunk_id)
    old_text, req_id, section, ordinal, document_id, project = row
    if _hash(old_text) == _hash(new_text):
        return {"chunk_id": chunk_id, "req_id": req_id, "project": project, "changed": False, "chunks": 1}

    ensure_baseline(document_id, changed_by)        # snapshot approved state, flip to draft (once)
    pieces = _split(new_text)                       # 1, or several if over the ceiling
    vectors, _tokens = embed_documents(pieces)      # OUTSIDE the write tx

    with pool().connection() as conn, conn.cursor() as cur:
        if len(pieces) > 1:
            _defer_ordinals(cur)
            cur.execute(
                "update brd_chunk set ordinal = ordinal + %s where document_id = %s and ordinal > %s",
                (len(pieces) - 1, document_id, ordinal),
            )
        cur.execute(
            "update brd_chunk set text = %s, token_count = %s, content_hash = %s where id = %s",
            (pieces[0], _est_tokens(pieces[0]), _hash(pieces[0]), chunk_id),
        )
        cur.execute(
            """insert into brd_embedding (chunk_id, model, embedding) values (%s, %s, %s::vector)
               on conflict (chunk_id) do update set model = excluded.model, embedding = excluded.embedding""",
            (chunk_id, settings.embed_model_docs, _vec_literal(vectors[0])),
        )
        for i, piece in enumerate(pieces[1:], start=1):
            _insert_chunk(cur, document_id, req_id, section, ordinal + i, piece, _vec_literal(vectors[i]))
        _audit(cur, document_id, chunk_id, req_id, old_text, new_text, kind, changed_by)
        conn.commit()

    cache.bust_project(project)
    return {"chunk_id": chunk_id, "req_id": req_id, "project": project, "changed": True, "chunks": len(pieces)}


def add_requirement(owner_id: int, project: str, text: str, req_id: str | None = None,
                    section: str | None = None, after_chunk_id: int | None = None,
                    changed_by: int | None = None) -> dict:
    """Add a new requirement — after a given chunk, or appended to the BRD. Splits if
    over the chunk ceiling. Returns {chunk_id (first piece), req_id, project, added}."""
    text = (text or "").strip()
    if not text:
        raise ValueError("text is empty")

    with pool().connection() as conn, conn.cursor() as cur:
        if after_chunk_id is not None:
            cur.execute(
                """select c.ordinal, c.document_id from brd_chunk c
                   join brd_document d on d.id = c.document_id
                   where c.id = %s and d.owner_id = %s and d.project = %s""",
                (after_chunk_id, owner_id, project),
            )
            r = cur.fetchone()
            if not r:
                raise ChunkNotFound(after_chunk_id)
            insert_at, document_id = r[0] + 1, r[1]
        else:
            cur.execute(
                "select id from brd_document where owner_id = %s and project = %s order by id desc limit 1",
                (owner_id, project),
            )
            r = cur.fetchone()
            if not r:
                raise ChunkNotFound(project)
            document_id = r[0]
            cur.execute("select coalesce(max(ordinal), -1) from brd_chunk where document_id = %s", (document_id,))
            insert_at = cur.fetchone()[0] + 1

    ensure_baseline(document_id, changed_by)        # snapshot approved state, flip to draft (once)
    pieces = _split(text)
    vectors, _tokens = embed_documents(pieces)

    with pool().connection() as conn, conn.cursor() as cur:
        # make room only when inserting before the end
        cur.execute("select coalesce(max(ordinal), -1) from brd_chunk where document_id = %s", (document_id,))
        if insert_at <= cur.fetchone()[0]:
            _defer_ordinals(cur)
            cur.execute(
                "update brd_chunk set ordinal = ordinal + %s where document_id = %s and ordinal >= %s",
                (len(pieces), document_id, insert_at),
            )
        first_id = None
        for i, piece in enumerate(pieces):
            cid = _insert_chunk(cur, document_id, req_id, section, insert_at + i, piece, _vec_literal(vectors[i]))
            first_id = first_id or cid
        _audit(cur, document_id, first_id, req_id, None, text, "add", changed_by)
        conn.commit()

    cache.bust_project(project)
    return {"chunk_id": first_id, "req_id": req_id, "project": project, "added": len(pieces)}


def revert_requirement(owner_id: int, chunk_id: int, changed_by: int | None = None) -> dict:
    """Safety net: restore a requirement to its PREVIOUS text (undo the last edit),
    from the audit trail. Re-embeds/re-indexes/busts like any edit and records a
    'revert' audit row. Returns {reverted, ...}. No-op if there's no prior version."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """select rc.old_text from requirement_change rc
               join brd_document d on d.id = rc.document_id
               where rc.chunk_id = %s and d.owner_id = %s
                 and rc.old_text is not null and rc.old_text <> ''
               order by rc.changed_at desc limit 1""",
            (chunk_id, owner_id),
        )
        row = cur.fetchone()
    if not row:
        return {"reverted": False, "chunk_id": chunk_id}
    res = update_requirement(owner_id, chunk_id, row[0], changed_by=changed_by, kind="revert")
    return {"reverted": res.get("changed", False), **res}


def remove_requirement(owner_id: int, chunk_id: int, changed_by: int | None = None) -> dict:
    """Delete a requirement (its embedding cascades). Ordinals keep their gaps — order
    is preserved and no shift is needed. Returns {removed, req_id, project}."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """select c.text, c.req_id, c.document_id, d.project
               from brd_chunk c join brd_document d on d.id = c.document_id
               where c.id = %s and d.owner_id = %s""",
            (chunk_id, owner_id),
        )
        row = cur.fetchone()
    if not row:
        raise ChunkNotFound(chunk_id)
    old_text, req_id, document_id, project = row

    ensure_baseline(document_id, changed_by)         # snapshot approved state, flip to draft (once)

    with pool().connection() as conn, conn.cursor() as cur:
        # audit before delete (chunk_id FK is ON DELETE SET NULL, so it nulls afterwards)
        _audit(cur, document_id, chunk_id, req_id, old_text, "", "delete", changed_by)
        cur.execute("delete from brd_chunk where id = %s", (chunk_id,))   # cascades brd_embedding
        conn.commit()

    cache.bust_project(project)
    return {"removed": True, "req_id": req_id, "project": project}

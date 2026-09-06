"""In-place requirement (chunk) editing — QA "change a requirement", Phase 1.

Locate a requirement by chunk id, replace its text, re-embed it, and let the
indexes follow: `search_tsv` (a generated column) recomputes automatically, and
the HNSW vector index updates when the embedding is rewritten. Every real edit:
  * writes chunk text + new embedding in ONE transaction (they never disagree),
  * busts the project's retrieve+answer caches — the chunk id is unchanged but its
    text is not, and the answer cache is keyed on chunk ids, so it MUST be busted,
  * records an audit row (who changed what, old → new, when) for QA traceability.

Phase 1 edits one chunk's text in place. Splitting an over-long edit into multiple
chunks (chunk-boundary drift) is Phase 2.
"""
from __future__ import annotations

from .. import cache
from ..config import settings
from ..db import pool
from .chunker import _est_tokens, _hash
from .embedder import embed_documents
from .store import _vec_literal


class ChunkNotFound(Exception):
    """No such chunk for this owner (missing, or not owned by the user)."""


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
            """select rc.id, rc.old_text, rc.new_text, rc.changed_at, u.email
               from requirement_change rc
               join brd_document d on d.id = rc.document_id
               left join app_user u on u.id = rc.changed_by
               where rc.chunk_id = %s and d.owner_id = %s
               order by rc.changed_at desc""",
            (chunk_id, owner_id),
        )
        return [{"id": r[0], "old_text": r[1], "new_text": r[2],
                 "changed_at": r[3].isoformat(), "changed_by": r[4]} for r in cur.fetchall()]


def update_requirement(owner_id: int, chunk_id: int, new_text: str,
                       changed_by: int | None = None) -> dict:
    """Replace a requirement's text → re-embed → re-index → bust caches → audit.

    Returns {chunk_id, req_id, project, changed}. `changed` is False when the text
    is identical (no re-embed, no bust). Raises ChunkNotFound / ValueError.
    """
    new_text = (new_text or "").strip()
    if not new_text:
        raise ValueError("new text is empty")

    # 1) load + authorize (owner must own the parent document)
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

    new_hash = _hash(new_text)
    if _hash(old_text) == new_hash:                 # idempotent: nothing to do
        return {"chunk_id": chunk_id, "req_id": req_id, "project": project, "changed": False}

    # 2) embed OUTSIDE the write tx (slow network call — never hold a row lock over it)
    vectors, _tokens = embed_documents([new_text])
    vec_lit = _vec_literal(vectors[0])

    # 3) one tx: chunk text + embedding + audit stay consistent
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update brd_chunk set text = %s, token_count = %s, content_hash = %s where id = %s",
            (new_text, _est_tokens(new_text), new_hash, chunk_id),
        )
        cur.execute(
            """insert into brd_embedding (chunk_id, model, embedding)
               values (%s, %s, %s::vector)
               on conflict (chunk_id) do update set
                 model = excluded.model, embedding = excluded.embedding""",
            (chunk_id, settings.embed_model_docs, vec_lit),
        )
        cur.execute(
            """insert into requirement_change
                 (document_id, chunk_id, req_id, old_text, new_text, changed_by)
               values (%s, %s, %s, %s, %s, %s)""",
            (document_id, chunk_id, req_id, old_text, new_text, changed_by),
        )
        cur.execute("update brd_document set updated_at = now() where id = %s", (document_id,))
        conn.commit()

    # 4) search_tsv recomputed automatically (generated column); bust corpus caches
    cache.bust_project(project)   # retrieve + answer for this project
    return {"chunk_id": chunk_id, "req_id": req_id, "project": project, "changed": True}

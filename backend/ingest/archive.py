"""Non-destructive requirement deletes (the ONLY way a live requirement leaves a BRD).

`retire` moves a live brd_chunk row — with its ORIGINAL id and its vector — into
brd_chunk_archive; `revive` moves it back under the same id. Nothing else in the app
hard-deletes a requirement: the revision apply, the requirement editor's delete, a snapshot
restore and a draft discard all go through here. Consequences:

  * every reference keeps resolving — use_case.source_chunk_ids, message.cited_chunk_ids,
    requirement_change.chunk_id, change proposals, cached requirement titles;
  * live retrieval / requirement lists / use-case generation stop seeing it (they read
    brd_chunk only), and the HNSW index is not polluted by dead vectors;
  * a restore brings the SAME requirement back (same id, same vector — no re-embedding).

Rows leave the archive only when the whole BRD is deleted (ON DELETE CASCADE from
brd_document). All functions run in the caller's transaction.
"""
from __future__ import annotations


def retire(cur, chunk_id: int, *, reason: str, by: int | None = None, revision_id: int | None = None) -> bool:
    """Move a live requirement to the archive (same id). Returns False if it wasn't live."""
    cur.execute(
        """insert into brd_chunk_archive
             (id, document_id, req_id, section, section_path, page, ordinal, text, token_count,
              content_hash, model, embedding, embed_hash, retired_by, reason, revision_id)
           select c.id, c.document_id, c.req_id, c.section, c.section_path, c.page, c.ordinal, c.text,
                  c.token_count, c.content_hash, e.model, e.embedding, c.embed_hash, %s, %s, %s
           from brd_chunk c left join brd_embedding e on e.chunk_id = c.id
           where c.id = %s
           on conflict (id) do update set
             ordinal = excluded.ordinal, text = excluded.text, content_hash = excluded.content_hash,
             model = excluded.model, embedding = excluded.embedding, embed_hash = excluded.embed_hash,
             retired_at = now(), retired_by = excluded.retired_by, reason = excluded.reason,
             revision_id = excluded.revision_id""",
        (by, reason, revision_id, chunk_id),
    )
    if cur.rowcount == 0:
        return False
    cur.execute("delete from brd_chunk where id = %s", (chunk_id,))      # cascades brd_embedding
    return True


def revive(cur, chunk_id: int, *, ordinal: int) -> bool:
    """Move an archived requirement back to live under its ORIGINAL id at `ordinal`
    (ordinals must be deferred by the caller). Returns False if it isn't archived."""
    cur.execute(
        """insert into brd_chunk (id, document_id, req_id, section, section_path, page, ordinal, text,
                                  token_count, content_hash, embed_hash)
           overriding system value
           select id, document_id, req_id, section, section_path, page, %s, text, token_count,
                  content_hash, embed_hash
           from brd_chunk_archive where id = %s
           returning id""",
        (ordinal, chunk_id),
    )
    if not cur.fetchone():
        return False
    cur.execute(
        """insert into brd_embedding (chunk_id, model, embedding)
           select id, model, embedding from brd_chunk_archive
           where id = %s and embedding is not null and model is not null""",
        (chunk_id,),
    )
    cur.execute("delete from brd_chunk_archive where id = %s", (chunk_id,))
    return True


def lookup(cur, ids: list[int]) -> dict[int, dict]:
    """Archived requirements by id (for citations / evidence of removed requirements)."""
    if not ids:
        return {}
    cur.execute(
        "select a.id, a.req_id, a.section, d.project, a.text, a.retired_at, a.reason "
        "from brd_chunk_archive a join brd_document d on d.id = a.document_id where a.id = any(%s)",
        (list(ids),),
    )
    return {r[0]: {"chunk_id": r[0], "req_id": r[1], "section": r[2], "project": r[3], "text": r[4],
                   "retired_at": r[5].isoformat(), "reason": r[6]} for r in cur.fetchall()}

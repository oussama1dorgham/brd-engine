"""Idempotent upsert of a parsed+chunked BRD into Postgres.

Two short transactions with the (slow, network-bound) embedding step in between,
so a DB connection is never held open while we call Voyage:

  tx1  read : upsert the document row, load which chunks already have embeddings
  ---- embed (no DB connection held) ----
  tx2  write: upsert chunks + embeddings, delete stale chunks

The diff logic is the point: a chunk is re-embedded only when it is new or its
content_hash changed. Re-ingesting an unchanged document embeds nothing.
"""
from __future__ import annotations

from .. import cache
from ..config import settings
from ..db import pool
from ..models import Chunk, DocumentMeta
from .embedder import embed_documents


def _vec_literal(vec: list[float]) -> str:
    """Format a Python vector as a pgvector text literal: [0.1,0.2,...]."""
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


def upsert(meta: DocumentMeta, chunks: list[Chunk], doc_sha: str) -> tuple[int, dict]:
    stats = {"total": len(chunks), "embedded": 0, "skipped": 0, "deleted": 0, "tokens": 0}

    # --- tx1: document row + existing embedded hashes (short) ---
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """insert into brd_document (title, project, version, status, sha256)
               values (%s, %s, %s, %s, %s)
               on conflict (project, title, version)
               do update set status = excluded.status, sha256 = excluded.sha256
               returning id""",
            (meta.title, meta.project, meta.version, meta.status, doc_sha),
        )
        doc_id = cur.fetchone()[0]
        cur.execute(
            """select c.ordinal, c.content_hash
               from brd_chunk c join brd_embedding e on e.chunk_id = c.id
               where c.document_id = %s""",
            (doc_id,),
        )
        existing = {ordinal: h for ordinal, h in cur.fetchall()}

    # --- embed only new/changed chunks (NO DB connection held) ---
    to_embed = [c for c in chunks if existing.get(c.ordinal) != c.content_hash]
    stats["skipped"] = len(chunks) - len(to_embed)
    vectors_by_ordinal: dict[int, list[float]] = {}
    if to_embed:
        vectors, tokens = embed_documents([c.text for c in to_embed])
        stats["tokens"] = tokens
        vectors_by_ordinal = {c.ordinal: v for c, v in zip(to_embed, vectors)}

    # --- tx2: write chunks + embeddings, drop stale (short) ---
    with pool().connection() as conn, conn.cursor() as cur:
        for c in chunks:
            cur.execute(
                """insert into brd_chunk
                     (document_id, req_id, section, ordinal, text, token_count, content_hash)
                   values (%s, %s, %s, %s, %s, %s, %s)
                   on conflict (document_id, ordinal) do update set
                     req_id = excluded.req_id, section = excluded.section,
                     text = excluded.text, token_count = excluded.token_count,
                     content_hash = excluded.content_hash
                   returning id""",
                (doc_id, c.req_id, c.section, c.ordinal, c.text, c.token_count, c.content_hash),
            )
            chunk_id = cur.fetchone()[0]
            if c.ordinal in vectors_by_ordinal:
                cur.execute(
                    """insert into brd_embedding (chunk_id, model, embedding)
                       values (%s, %s, %s::vector)
                       on conflict (chunk_id) do update set
                         model = excluded.model, embedding = excluded.embedding""",
                    (chunk_id, settings.embed_model_docs, _vec_literal(vectors_by_ordinal[c.ordinal])),
                )
                stats["embedded"] += 1

        cur.execute(
            "delete from brd_chunk where document_id = %s and ordinal >= %s",
            (doc_id, len(chunks)),
        )
        stats["deleted"] = max(cur.rowcount, 0)

    # Bust the project's corpus-dependent caches (retrieve/answer) whenever chunks
    # actually changed — an unchanged re-ingest embeds/deletes nothing and keeps the
    # cache warm. 'embed' entries are immutable and never busted. Runs after the
    # write tx has committed, so a hit can never resurrect pre-change results.
    if stats["embedded"] or stats["deleted"]:
        cache.bust_project(meta.project)

    return doc_id, stats

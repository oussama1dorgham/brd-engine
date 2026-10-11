"""Idempotent upsert of a parsed+chunked BRD into Postgres.

This is the Markdown/demo path — see backend/ingest/pipeline.py's module docstring.
It predates ingestion v2 and is kept ONLY for the synthetic `brd` demo DB (payments/
reporting samples used by the golden-set eval): no owner_id, no object storage, no
`ingest_job`, no `brd_chunk_archive`. Real BRDs go through backend.ingest.jobs.submit.

Two short transactions with the (slow, network-bound) embedding step in between,
so a DB connection is never held open while we call Voyage:

  tx1  read : upsert the document row, load which chunks already have embeddings
  ---- embed (no DB connection held) ----
  tx2  write: upsert chunks + embeddings

The diff logic is the point: a chunk is re-embedded only when it is new or its
content_hash changed. Re-ingesting an unchanged document embeds nothing.

NEVER hard-deletes: `brd_chunk_archive` doesn't exist on the demo DB, so a re-ingest
that would need to remove trailing chunks (the new file has fewer requirements than
are already stored) is refused with `ShrinkRejected` instead of deleting them.
"""
from __future__ import annotations

from backend.core import cache
from backend.core.config import settings
from backend.core.db import pool
from backend.ingest.models import Chunk, DocumentMeta
from .embedder import embed_documents


class ShrinkRejected(ValueError):
    """Raised instead of deleting trailing chunks — this path never hard-deletes."""


class WrongDatabase(RuntimeError):
    """Raised when this legacy demo-only path is pointed at a real (ingestion-v2) database —
    its `document_id, ordinal` upsert can't honor brd_real's DEFERRABLE unique constraint, and
    it would commit an ownerless brd_document. Detected by the presence of brd_chunk_archive,
    which only ever exists on a post-ingestion-v2 (real) database."""


def _refuse_if_real_db(cur) -> None:
    cur.execute("select to_regclass('brd_chunk_archive') is not null")
    if cur.fetchone()[0]:
        raise WrongDatabase("The Markdown path is only for the demo DB; upload .pdf/.docx instead.")


def _vec_literal(vec: list[float]) -> str:
    """Format a Python vector as a pgvector text literal: [0.1,0.2,...]."""
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


def upsert(meta: DocumentMeta, chunks: list[Chunk], doc_sha: str) -> tuple[int, dict]:
    stats = {"total": len(chunks), "embedded": 0, "skipped": 0, "tokens": 0}

    # --- tx1: document row + existing embedded hashes (short) ---
    with pool().connection() as conn, conn.cursor() as cur:
        _refuse_if_real_db(cur)
        cur.execute(
            "select id from brd_document where project = %s and title = %s and version = %s",
            (meta.project, meta.title, meta.version),
        )
        row = cur.fetchone()
        if row:
            doc_id = row[0]
            cur.execute("select count(*) from brd_chunk where document_id = %s", (doc_id,))
            existing_total = cur.fetchone()[0]
            if existing_total > len(chunks):
                raise ShrinkRejected(
                    f"{meta.project}/{meta.title} {meta.version} has {existing_total} stored "
                    f"requirements; the new file has only {len(chunks)}. This path never "
                    f"hard-deletes, so re-ingesting would have to drop the difference — refused. "
                    f"Bump the version, or edit the stored rows directly if this is intentional."
                )
            cur.execute(
                "update brd_document set status = %s, sha256 = %s where id = %s",
                (meta.status, doc_sha, doc_id),
            )
        else:
            cur.execute(
                """insert into brd_document (title, project, version, status, sha256)
                   values (%s, %s, %s, %s, %s) returning id""",
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
        vectors, tokens = embed_documents([c.text for c in to_embed], priority="ingest")
        stats["tokens"] = tokens
        vectors_by_ordinal = {c.ordinal: v for c, v in zip(to_embed, vectors)}

    # --- tx2: write chunks + embeddings (short) --- no delete: tx1 already refused any
    # re-ingest that would need to drop trailing chunks, so there is never anything stale here.
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

    # Bust the project's corpus-dependent caches (retrieve/answer) whenever chunks
    # actually changed — an unchanged re-ingest embeds nothing and keeps the cache warm.
    # 'embed' entries are immutable and never busted. Runs after the write tx has
    # committed, so a hit can never resurrect pre-change results.
    if stats["embedded"]:
        cache.bust_project(meta.project)

    return doc_id, stats

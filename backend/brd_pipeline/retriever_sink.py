"""Wire the pipeline's chunks + vectors into the EXISTING retriever tables.

Writes into `brd_document` / `brd_chunk` / `brd_embedding` — the same tables the
UI's BRD selector (`history_store.list_projects`) and `retrieve/retriever.py`
already read — using the app's psycopg3 pool (`src/db.py`).

Transactional integrity: the whole write runs inside ONE `pool().connection()`
block, which commits on clean exit and rolls back on error (the pattern proven
in `src/ingest/store.py`). Therefore:
  * no orphaned chunks/embeddings on failure, and
  * `list_projects()` never sees a half-ingested BRD — the `brd_document` row
    only becomes visible on commit (atomicity, not a status flag, guarantees it).

Re-ingesting the same (project, title, version) replaces its chunks (idempotent).
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Sequence

from ..config import settings
from ..db import pool
from .chunking import Chunk

logger = logging.getLogger(__name__)

# Requirement id like "FR-14", "NFR 3", "BR-2a" — improves citations/FTS.
# Searched (not anchored) over the chunk's head so an id is found even when a
# heading or lead-in sentence precedes it.
_REQ_ID = re.compile(r"\b((?:FR|NFR|BR|UR|SR)[-\s]?\d+[A-Za-z]?)\b", re.IGNORECASE)


def _vec_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


def _req_id_of(text: str) -> str | None:
    m = _REQ_ID.search(text[:120])
    return m.group(1).upper().replace(" ", "-") if m else None


def slugify_project(title: str) -> str:
    """Turn a title into a stable project id the UI lists in its BRD dropdown."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "brd"


def store_into_retriever(
    *,
    title: str,
    project: str,
    chunks: Sequence[Chunk],
    vectors: Sequence[Sequence[float]],
    owner_id: int,
    model: str | None = None,
    version: str = "v1",
    status: str = "ready",
    doc_sha: str | None = None,
) -> int:
    """Insert one BRD into the retriever tables in a single transaction. Returns doc_id."""
    if len(chunks) != len(vectors):
        raise ValueError(f"chunks/vectors length mismatch: {len(chunks)} != {len(vectors)}")
    if not chunks:
        raise ValueError("refusing to register a BRD with zero chunks")

    model = settings.need(model or settings.embed_model_docs, "EMBED_MODEL_DOCS")
    if doc_sha is None:
        doc_sha = hashlib.sha256("".join(c.text for c in chunks).encode("utf-8")).hexdigest()

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """insert into brd_document (title, project, version, status, sha256, owner_id)
               values (%s, %s, %s, %s, %s, %s)
               on conflict (owner_id, project, title, version)
               do update set status = excluded.status, sha256 = excluded.sha256
               returning id""",
            (title, project, version, status, doc_sha, owner_id),
        )
        doc_id = cur.fetchone()[0]

        # Replace prior chunks for idempotent re-ingest (cascades to embeddings).
        cur.execute("delete from brd_chunk where document_id = %s", (doc_id,))
        _insert_chunks(cur, doc_id, chunks, vectors, model)

    logger.info("store_into_retriever: project=%s doc_id=%s chunks=%d", project, doc_id, len(chunks))
    return doc_id


def _insert_chunks(cur, doc_id: int, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]], model: str) -> None:
    for c, vec in zip(chunks, vectors):
        content_hash = hashlib.sha256(c.text.encode("utf-8")).hexdigest()
        cur.execute(
            """insert into brd_chunk
                 (document_id, req_id, section, ordinal, text, token_count, content_hash)
               values (%s, %s, %s, %s, %s, %s, %s)
               returning id""",
            (doc_id, _req_id_of(c.text), None, c.index, c.text, c.token_count, content_hash),
        )
        chunk_id = cur.fetchone()[0]
        cur.execute(
            """insert into brd_embedding (chunk_id, model, embedding)
               values (%s, %s, %s::vector)""",
            (chunk_id, model, _vec_literal(vec)),
        )


def create_processing_document(*, owner_id: int, title: str, project: str,
                               version: str = "v1", doc_sha: str | None = None) -> int:
    """Create/reset a BRD row in 'processing' state (no chunks yet). Returns doc_id.

    Used by the background-ingest path so an upload can return immediately; the
    worker then calls `populate_document` to add chunks and flip it to 'ready'.
    """
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """insert into brd_document (title, project, version, status, sha256, owner_id, chunks_done, chunks_total, proc_started_at)
               values (%s, %s, %s, 'processing', %s, %s, 0, null, now())
               on conflict (owner_id, project, title, version)
               do update set status = 'processing', sha256 = excluded.sha256,
                             chunks_done = 0, chunks_total = null, proc_started_at = now()
               returning id""",
            (title, project, version, doc_sha or "pending", owner_id),
        )
        doc_id = cur.fetchone()[0]
        cur.execute("delete from brd_chunk where document_id = %s", (doc_id,))   # clear any prior chunks
    return doc_id


def populate_document(*, doc_id: int, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]],
                      model: str | None = None) -> None:
    """Insert chunks + embeddings for an existing doc and mark it 'ready' (one tx)."""
    if len(chunks) != len(vectors):
        raise ValueError("chunks/vectors length mismatch")
    model = settings.need(model or settings.embed_model_docs, "EMBED_MODEL_DOCS")
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("delete from brd_chunk where document_id = %s", (doc_id,))
        _insert_chunks(cur, doc_id, chunks, vectors, model)
        cur.execute(
            "update brd_document set status = 'ready', chunks_done = %s, chunks_total = %s where id = %s",
            (len(chunks), len(chunks), doc_id),
        )
    logger.info("populate_document: doc_id=%s chunks=%d -> ready", doc_id, len(chunks))


def set_document_status(doc_id: int, status: str) -> None:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update brd_document set status = %s where id = %s", (status, doc_id))


def set_document_progress(doc_id: int, done: int, total: int) -> None:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update brd_document set chunks_done = %s, chunks_total = %s where id = %s", (done, total, doc_id))

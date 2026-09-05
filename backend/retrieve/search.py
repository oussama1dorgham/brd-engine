"""Minimal semantic search over ingested BRD chunks (seed of Phase 2).

Embeds the query with voyage-4-lite and ranks chunks by cosine distance using
the pgvector HNSW index. Optional project filter narrows the scope.

    python -m backend.retrieve.search "How do users authenticate to the payments admin?"
"""
from __future__ import annotations

import sys

from ..db import pool
from ..ingest.embedder import embed_query


def search(query: str, k: int = 5, project: str | None = None) -> list[tuple]:
    qvec, _ = embed_query(query)
    vec_literal = "[" + ",".join(f"{x:.8f}" for x in qvec) + "]"

    where = "where d.project = %s" if project else ""
    sql = f"""
        select c.req_id, c.section, d.project, c.text,
               e.embedding <=> %s::vector as distance
        from brd_chunk c
        join brd_embedding e on e.chunk_id = c.id
        join brd_document d on d.id = c.document_id
        {where}
        order by distance
        limit %s
    """
    params: list = [vec_literal]
    if project:
        params.append(project)
    params.append(k)

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _parse_args(argv: list[str]) -> tuple[str, str | None]:
    project: str | None = None
    words: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--project" and i + 1 < len(argv):
            project = argv[i + 1]
            i += 2
        elif a.startswith("--project="):
            project = a.split("=", 1)[1]
            i += 1
        else:
            words.append(a)
            i += 1
    return " ".join(words), project


if __name__ == "__main__":
    q, project = _parse_args(sys.argv[1:])
    q = q or "How do users authenticate to the payments admin?"
    print(f"query: {q!r}   project={project or 'ALL'}\n")
    print(f"{'dist':>6}  {'req_id':<7}  {'project':<10}  preview")
    print("-" * 84)
    for req_id, section, proj, text, dist in search(q, k=4, project=project):
        print(f"{dist:>6.3f}  {req_id or '-':<7}  {proj:<10}  {text.replace(chr(10), ' ')[:48]}")

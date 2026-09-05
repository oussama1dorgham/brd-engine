"""Keyword (full-text) search over BRD chunks — the BM25-style half of hybrid.

Vectors are fuzzy about exact tokens; this catches literal matches a user is
likely to type: requirement IDs (FR-14), acronyms (SSO), formats (CSV, PDF).
It searches over `req_id + text` so an ID typed as a query still hits, even
though the chunk body itself never repeats the ID.

    python -m backend.retrieve.keyword "FR-14"
    python -m backend.retrieve.keyword "export to CSV" --project reporting

Note: the tsvector is computed inline here (fine at this scale). At large scale
this becomes a stored generated column with its own GIN index.
"""
from __future__ import annotations

import sys

from ..db import pool


def keyword_search(query: str, k: int = 5, project: str | None = None) -> list[tuple]:
    where = "where c.search_tsv @@ plainto_tsquery('simple', %s)"
    params: list = [query, query]  # first for ts_rank, second for the where match
    if project:
        where += " and d.project = %s"
        params.append(project)
    params.append(k)

    sql = f"""
        select c.req_id, c.section, d.project, c.text,
               ts_rank(c.search_tsv, plainto_tsquery('simple', %s)) as rank
        from brd_chunk c
        join brd_document d on d.id = c.document_id
        {where}
        order by rank desc
        limit %s
    """
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _parse_args(argv: list[str]) -> tuple[str, str | None]:
    project, words, i = None, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--project" and i + 1 < len(argv):
            project, i = argv[i + 1], i + 2
        elif a.startswith("--project="):
            project, i = a.split("=", 1)[1], i + 1
        else:
            words.append(a)
            i += 1
    return " ".join(words), project


if __name__ == "__main__":
    q, project = _parse_args(sys.argv[1:])
    print(f"keyword: {q!r}   project={project or 'ALL'}\n")
    print(f"{'rank':>6}  {'req_id':<7}  {'project':<10}  preview")
    print("-" * 84)
    rows = keyword_search(q, k=5, project=project)
    if not rows:
        print("  (no keyword matches)")
    for req_id, section, proj, text, rank in rows:
        print(f"{rank:>6.3f}  {req_id or '-':<7}  {proj:<10}  {text.replace(chr(10), ' ')[:48]}")

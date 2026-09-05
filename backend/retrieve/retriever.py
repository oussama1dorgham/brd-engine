"""Unified retriever: vector + keyword -> RRF fusion -> Voyage rerank.

This is the function the chatbot (Phase 3) will call. Pipeline:
  1. embed the query (voyage-4-lite) and pull top vector hits
  2. pull top keyword (FTS) hits
  3. fuse the two rank lists with Reciprocal Rank Fusion (RRF)
  4. rerank the fused candidates with Voyage rerank-2.5 and keep top-n

    python -m backend.retrieve.retriever "what does FR-14 require about login?" --project payments
    python -m backend.retrieve.retriever "..." --no-rerank      # compare fusion-only
"""
from __future__ import annotations

import sys

from .. import cache
from ..db import pool
from ..ingest.embedder import embed_query
from .rerank import rerank


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


def _keyword_rows(cur, query: str, owner_id: int, project: str | None, limit: int) -> list[tuple]:
    where = "where d.owner_id = %s and d.status = 'ready' and c.search_tsv @@ plainto_tsquery('simple', %s)"
    params: list = [owner_id, query]
    if project:
        where += " and d.project = %s"
        params.append(project)
    params.append(query)   # ts_rank in ORDER BY
    params.append(limit)
    cur.execute(
        f"""select c.id, c.req_id, c.section, d.project, c.text
            from brd_chunk c
            join brd_document d on d.id = c.document_id
            {where}
            order by ts_rank(c.search_tsv, plainto_tsquery('simple', %s)) desc
            limit %s""",
        params,
    )
    return cur.fetchall()


def _rrf(rank_lists: list[list[int]], k: int = 60) -> dict[int, float]:
    scores: dict[int, float] = {}
    for lst in rank_lists:
        for rank, cid in enumerate(lst):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return scores


def retrieve(query: str, *, owner_id: int, k_final: int = 5, candidates: int = 10,
             project: str | None = None, use_rerank: bool = True,
             query_vec: list[float] | None = None) -> list[dict]:
    # Retrieval cache: a hit returns the ranked results and skips embed + rerank
    # entirely. Keyed on everything that changes the ranking; corpus changes are
    # handled by cache.bust_project() on ingest/delete (TTL is only a safety net).
    # Checked BEFORE any pool checkout so we never nest connections.
    cache_parts = [query, owner_id, project, k_final, candidates, use_rerank]
    hit = cache.get(cache.RETRIEVE, cache_parts)
    if hit is not None:
        return hit

    if query_vec is None:
        query_vec, _ = embed_query(query)
    vec_lit = _vec_literal(query_vec)

    with pool().connection() as conn, conn.cursor() as cur:
        # Always scope to the owner; add the project filter when a BRD is selected.
        vwhere = "where d.owner_id = %s and d.status = 'ready'"
        vparams: list = [owner_id]
        if project:
            vwhere += " and d.project = %s"
            vparams.append(project)
        vparams += [vec_lit, 20]
        cur.execute(
            f"""select c.id, c.req_id, c.section, d.project, c.text
                from brd_chunk c
                join brd_embedding e on e.chunk_id = c.id
                join brd_document d on d.id = c.document_id
                {vwhere}
                order by e.embedding <=> %s::vector
                limit %s""",
            vparams,
        )
        vec_rows = cur.fetchall()
        kw_rows = _keyword_rows(cur, query, owner_id, project, 20)

    info = {r[0]: r for r in vec_rows}
    for r in kw_rows:
        info.setdefault(r[0], r)

    vec_ids = [r[0] for r in vec_rows]
    kw_ids = [r[0] for r in kw_rows]
    fused = _rrf([vec_ids, kw_ids])
    ranked = sorted(fused, key=lambda cid: fused[cid], reverse=True)[:candidates]

    if use_rerank and ranked:
        docs = [info[cid][4] for cid in ranked]           # index 4 = text
        order = rerank(query, docs, top_k=k_final)
        chosen = [(ranked[i], score) for i, score in order]
    else:
        chosen = [(cid, fused[cid]) for cid in ranked[:k_final]]

    results = []
    for cid, score in chosen:
        _id, req_id, section, proj, text = info[cid]
        results.append({"chunk_id": cid, "req_id": req_id, "section": section,
                        "project": proj, "text": text, "score": score})
    # set() after the connection block closes — never inside a pool checkout.
    cache.set(cache.RETRIEVE, cache_parts, results, owner_id=owner_id, project=project)
    return results


def _parse_args(argv: list[str]) -> tuple[str, str | None, bool, int | None]:
    project, use_rerank, owner, words, i = None, True, None, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--project" and i + 1 < len(argv):
            project, i = argv[i + 1], i + 2
        elif a.startswith("--project="):
            project, i = a.split("=", 1)[1], i + 1
        elif a == "--owner" and i + 1 < len(argv):
            owner, i = int(argv[i + 1]), i + 2
        elif a.startswith("--owner="):
            owner, i = int(a.split("=", 1)[1]), i + 1
        elif a == "--no-rerank":
            use_rerank, i = False, i + 1
        else:
            words.append(a)
            i += 1
    return " ".join(words), project, use_rerank, owner


if __name__ == "__main__":
    q, project, use_rerank, owner = _parse_args(sys.argv[1:])
    if owner is None:
        print("error: --owner <app_user.id> is required")
        sys.exit(2)
    mode = "hybrid+rerank" if use_rerank else "hybrid (RRF only)"
    print(f"query: {q!r}   project={project or 'ALL'}   mode={mode}\n")
    print(f"{'score':>7}  {'req_id':<7}  {'project':<10}  preview")
    print("-" * 84)
    for r in retrieve(q, owner_id=owner, project=project, use_rerank=use_rerank):
        print(f"{r['score']:>7.3f}  {r['req_id'] or '-':<7}  {r['project']:<10}  "
              f"{r['text'].replace(chr(10), ' ')[:46]}")

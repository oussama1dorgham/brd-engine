"""Voyage rerank-2.5 wrapper — cross-encoder re-scoring of candidates.

Given a query and candidate texts, returns the best `top_k` as
(original_index, relevance_score), most relevant first. Throttled via guarded().
"""
from __future__ import annotations

from ..config import settings
from ..voyage import client, guarded


def rerank(query: str, documents: list[str], top_k: int | None = None,
           model: str | None = None) -> list[tuple[int, float]]:
    if not documents:
        return []
    model = settings.need(model or settings.rerank_model, "RERANK_MODEL")
    res = guarded(client().rerank, query, documents, model=model, top_k=top_k)
    return [(r.index, r.relevance_score) for r in res.results]

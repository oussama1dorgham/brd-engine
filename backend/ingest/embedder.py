"""Voyage AI embedding helpers.

Documents and queries share a vector space (voyage-4 / voyage-4-lite). All calls
go through the throttled `guarded()` wrapper so the free-tier RPM cap never
crashes a run.
"""
from __future__ import annotations

import os

from .. import cache
from ..config import settings
from ..voyage import client, guarded

# Free-tier Voyage caps at ~10K tokens/min; small batches keep each request under it.
_EMBED_BATCH = int(os.getenv("VOYAGE_EMBED_BATCH", "10"))


def embed_documents(texts: list[str], model: str | None = None, batch_size: int | None = None):
    """Return (list_of_vectors, total_tokens) for document chunks."""
    model = settings.need(model or settings.embed_model_docs, "EMBED_MODEL_DOCS")
    batch_size = batch_size or _EMBED_BATCH
    vectors: list[list[float]] = []
    total_tokens = 0
    for i in range(0, len(texts), batch_size):
        res = guarded(client().embed, texts[i : i + batch_size], model=model, input_type="document")
        vectors.extend(res.embeddings)
        total_tokens += res.total_tokens
    return vectors, total_tokens


def embed_queries(texts: list[str], model: str | None = None, batch_size: int = 128):
    """Return (list_of_vectors, total_tokens) for search queries (batched)."""
    model = settings.need(model or settings.embed_model_query, "EMBED_MODEL_QUERY")
    vectors: list[list[float]] = []
    total_tokens = 0
    for i in range(0, len(texts), batch_size):
        res = guarded(client().embed, texts[i : i + batch_size], model=model, input_type="query")
        vectors.extend(res.embeddings)
        total_tokens += res.total_tokens
    return vectors, total_tokens


def embed_query(text: str, model: str | None = None):
    """Return (vector, total_tokens) for a single query.

    Cached: a query embedding is a pure function of (text, model), so it never
    expires (ttl 0). A hit spends no Voyage budget, so it reports 0 tokens — the
    main defence against the free-tier 3-req/min cap on repeated questions.
    """
    model = settings.need(model or settings.embed_model_query, "EMBED_MODEL_QUERY")
    hit = cache.get(cache.EMBED, [text, model], ttl_seconds=0)
    if hit is not None:
        return hit, 0
    vectors, tokens = embed_queries([text], model=model)
    cache.set(cache.EMBED, [text, model], vectors[0])
    return vectors[0], tokens

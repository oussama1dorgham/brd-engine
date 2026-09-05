"""Stage 4 — Embedding (Voyage AI).

Reuses the project's throttled Voyage client (`src/voyage.py`) so free-tier RPM
caps never crash a run. The model name is configurable and the embedding
dimension is recorded from the returned vectors, so the pgvector column can be
defined consistently (must equal `PipelineConfig.embed_dim` / the `vector(N)` DDL).

Also exposes `voyage_token_counter`, the production token counter injected into
`chunk_text` so chunk sizes are measured with the embedding model's own tokenizer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from ..voyage import client, guarded
from .chunking import approx_token_counter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    dim: int
    total_tokens: int


def embed_chunks(texts: list[str], *, model: str, batch_size: int = 10,
                 on_progress: Callable[[int, int], None] | None = None) -> EmbeddingResult:
    """Embed document chunks in batches; return vectors + measured dimension.

    `on_progress(done, total)` is called after each batch (for the "Processing NN%" UI).
    """
    if not texts:
        return EmbeddingResult(vectors=[], dim=0, total_tokens=0)

    vectors: list[list[float]] = []
    total_tokens = 0
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        res = guarded(client().embed, batch, model=model, input_type="document")
        vectors.extend(res.embeddings)
        total_tokens += res.total_tokens
        if on_progress:
            on_progress(len(vectors), len(texts))
        logger.debug("embed_chunks: %d/%d embedded", len(vectors), len(texts))

    dim = len(vectors[0]) if vectors else 0
    logger.info("embed_chunks: %d vectors, dim=%d, tokens=%d", len(vectors), dim, total_tokens)
    return EmbeddingResult(vectors=vectors, dim=dim, total_tokens=total_tokens)


def voyage_token_counter(model: str):
    """Return a `Callable[[str], int]` that counts tokens with Voyage's tokenizer.

    Falls back to the word-count proxy if the SDK build doesn't expose
    count_tokens with the expected signature, so chunking never hard-fails.
    """
    c = client()

    def _count(text: str) -> int:
        try:
            return int(c.count_tokens([text], model))
        except TypeError:
            try:
                return int(c.count_tokens([text]))
            except Exception:  # noqa: BLE001
                return approx_token_counter(text)
        except Exception:  # noqa: BLE001
            return approx_token_counter(text)

    return _count

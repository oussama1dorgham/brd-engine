"""Pipeline configuration — externalized via env vars, no hardcoded credentials.

`DATABASE_URL` and `VOYAGE_API_KEY` come from the shared `src.config.settings`
(single `.env` source of truth). Pipeline-specific knobs (chunk sizes, table
names, embedding model/dim) are read here with spec-sane defaults so the module
runs out of the box while staying overridable per-environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from ..config import settings


@dataclass(frozen=True)
class PipelineConfig:
    # --- DB (credentials always from env, never hardcoded) ---
    database_url: str = settings.database_url

    # --- Embedding (Voyage) ---
    # Model name is configurable; embed_dim MUST match the vector(N) column (migrations).
    embed_model: str = os.getenv("BRD_EMBED_MODEL", settings.embed_model_docs or "voyage-4")
    embed_dim: int = int(os.getenv("BRD_EMBED_DIM", "1024"))
    embed_batch: int = int(os.getenv("VOYAGE_EMBED_BATCH", "10"))

    # --- Chunking (token-based; counted with the embedding model's tokenizer) ---
    chunk_tokens: int = int(os.getenv("BRD_CHUNK_TOKENS", "500"))
    chunk_overlap: int = int(os.getenv("BRD_CHUNK_OVERLAP", "50"))


pipeline_config = PipelineConfig()

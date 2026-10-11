"""Standalone BRD ingestion pipeline (PDF/DOCX -> chunks -> Voyage vectors -> pgvector).

Self-contained and independent of `src/ingest/`. Stages are discrete, typed, and
individually testable: parsing, preprocessing, chunking, embedding, storage.

    from backend.legacy.brd_pipeline.pipeline import ingest
    result = ingest("data/brds/example.pdf")

See `ddl.sql` for the `brd_catalog` + `brd_chunks` schema. Run integration test:

    .venv\\Scripts\\python.exe -m backend.legacy.brd_pipeline.pipeline data/brds/example.pdf --title "Payments BRD"
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from backend.legacy.brd_pipeline.parsing import (
    ParsedDocument,
    parse_document,
    UnsupportedFileTypeError,
    DocumentParseError,
)
from backend.legacy.brd_pipeline.preprocessing import normalize_text, derive_title, new_brd_id
from backend.legacy.brd_pipeline.chunking import Chunk, chunk_text
from backend.legacy.brd_pipeline.embedding import EmbeddingResult, embed_chunks, voyage_token_counter

if TYPE_CHECKING:  # for type-checkers/IDEs only
    from backend.legacy.brd_pipeline.pipeline import ingest, IngestResult


def __getattr__(name: str):
    # Lazy re-export so importing the package doesn't pull in `pipeline`
    # (keeps `python -m backend.legacy.brd_pipeline.pipeline` free of a re-import warning).
    if name in ("ingest", "IngestResult"):
        from backend.legacy.brd_pipeline import pipeline

        return getattr(pipeline, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ParsedDocument",
    "parse_document",
    "UnsupportedFileTypeError",
    "DocumentParseError",
    "normalize_text",
    "derive_title",
    "new_brd_id",
    "Chunk",
    "chunk_text",
    "EmbeddingResult",
    "embed_chunks",
    "voyage_token_counter",
    "ingest",
    "IngestResult",
]

"""Orchestrator + CLI entrypoint for the BRD ingestion pipeline.

Flow:
    parse -> preprocess -> chunk -> embed (network) -> store (single DB transaction)

The Voyage embedding call runs BEFORE the DB write: it's a slow, rate-limited
network call, so we compute all vectors first, then do a short write. The write
itself (into brd_document / brd_chunk / brd_embedding) is one transaction via the
app's psycopg3 pool — see `retriever_sink.store_into_retriever` for the integrity
model (commit-on-clean-exit, so a half-ingested BRD is never visible to the UI).
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass

from .config import PipelineConfig, pipeline_config
from .embedding import embed_chunks, voyage_token_counter
from .chunking import chunk_text
from .parsing import Source, parse_document
from .preprocessing import derive_title, new_brd_id, normalize_text

logger = logging.getLogger(__name__)


class EmptyDocumentError(RuntimeError):
    """Raised when a document produces no chunks to embed."""


class EmbeddingDimensionError(RuntimeError):
    """Raised when the embedding dim doesn't match the configured pgvector column."""


@dataclass(frozen=True)
class IngestResult:
    brd_id: str
    title: str
    source_filename: str
    chunk_count: int
    embed_dim: int
    total_tokens: int
    status: str
    project: str | None = None       # the UI dropdown id
    document_id: int | None = None   # brd_document.id


def ingest(
    source: Source,
    *,
    owner_id: int,
    filename: str | None = None,
    title: str | None = None,
    project: str | None = None,
    config: PipelineConfig = pipeline_config,
) -> IngestResult:
    """Run the full pipeline for one BRD and store it in the retriever tables.

    Writes into the existing brd_document / brd_chunk / brd_embedding tables so the
    BRD appears in the UI's dropdown and is answerable by `retrieve/retriever.py`.
    `project` is the dropdown id (derived from the title if omitted).
    """
    # --- 1. Parse (pure) ---
    parsed = parse_document(source, filename=filename)
    logger.info("ingest: parsed %s (%s)", parsed.source_filename, parsed.file_type)

    # --- 2. Preprocess & metadata (pure) ---
    clean = normalize_text(parsed.text)
    brd_id = new_brd_id()
    resolved_title = derive_title(parsed.source_filename, override=title)

    # --- 3. Chunk (pure; token counts via the embedding model's tokenizer) ---
    counter = voyage_token_counter(config.embed_model)
    chunks = chunk_text(
        clean,
        max_tokens=config.chunk_tokens,
        overlap_tokens=config.chunk_overlap,
        count_tokens=counter,
    )
    if not chunks:
        raise EmptyDocumentError(f"{parsed.source_filename!r} produced no chunks.")

    # --- 4. Embed (network) — OUTSIDE any DB transaction ---
    emb = embed_chunks(
        [c.text for c in chunks], model=config.embed_model, batch_size=config.embed_batch
    )
    if emb.dim != config.embed_dim:
        raise EmbeddingDimensionError(
            f"Model returned dim {emb.dim} but config expects {config.embed_dim}. "
            f"Align BRD_EMBED_DIM and the vector(N) column."
        )

    # --- 5. Store into the UI/retriever tables (single transaction) ---
    from .retriever_sink import slugify_project, store_into_retriever

    resolved_project = project or slugify_project(resolved_title)
    document_id = store_into_retriever(
        title=resolved_title,
        project=resolved_project,
        chunks=chunks,
        vectors=emb.vectors,
        owner_id=owner_id,
        model=config.embed_model,
    )
    logger.info("ingest: registered project=%s doc_id=%s (%d chunks) for the UI",
                resolved_project, document_id, len(chunks))
    return IngestResult(
        brd_id=brd_id,
        title=resolved_title,
        source_filename=parsed.source_filename,
        chunk_count=len(chunks),
        embed_dim=emb.dim,
        total_tokens=emb.total_tokens,
        status="READY",
        project=resolved_project,
        document_id=document_id,
    )


def run_ingest_into(doc_id: int, source: Source, *, filename: str | None = None,
                    config: PipelineConfig = pipeline_config) -> int:
    """Parse -> chunk -> embed -> populate an EXISTING (processing) doc, mark ready.

    Used by the API's background-ingest worker. Raises on failure (the caller marks
    the document 'failed'). Returns the chunk count.
    """
    parsed = parse_document(source, filename=filename)
    clean = normalize_text(parsed.text)
    counter = voyage_token_counter(config.embed_model)
    chunks = chunk_text(clean, max_tokens=config.chunk_tokens,
                        overlap_tokens=config.chunk_overlap, count_tokens=counter)
    if not chunks:
        raise EmptyDocumentError(f"{parsed.source_filename!r} produced no chunks.")
    from .retriever_sink import populate_document, set_document_progress
    set_document_progress(doc_id, 0, len(chunks))
    emb = embed_chunks(
        [c.text for c in chunks], model=config.embed_model, batch_size=config.embed_batch,
        on_progress=lambda done, total: set_document_progress(doc_id, done, total),
    )
    if emb.dim != config.embed_dim:
        raise EmbeddingDimensionError(
            f"Model returned dim {emb.dim} but config expects {config.embed_dim}."
        )
    populate_document(doc_id=doc_id, chunks=chunks, vectors=emb.vectors, model=config.embed_model)
    return len(chunks)


def main() -> None:
    """CLI entrypoint for integration testing.

    Sample invocation:
        .venv\\Scripts\\python.exe -m backend.brd_pipeline.pipeline \\
            data/brds/payments.pdf --title "Payments BRD"

    Dry run (parse+chunk only, no embeddings, no DB):
        .venv\\Scripts\\python.exe -m backend.brd_pipeline.pipeline data/brds/payments.docx --dry-run
    """
    parser = argparse.ArgumentParser(description="Ingest a BRD (.pdf/.docx) into pgvector.")
    parser.add_argument("path", help="Path to the .pdf or .docx BRD file")
    parser.add_argument("--title", default=None, help="Override the derived BRD title")
    parser.add_argument("--project", default=None,
                        help="Project id for the UI dropdown (derived from title if omitted)")
    parser.add_argument("--owner-id", type=int, default=None, help="owning app_user.id (required unless --dry-run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse + chunk only; skip embeddings and DB writes")
    parser.add_argument("--log", default="INFO", help="Log level (DEBUG/INFO/WARNING)")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.dry_run:
        parsed = parse_document(args.path)
        clean = normalize_text(parsed.text)
        # Dry run uses the offline word-count proxy so it needs no Voyage key.
        chunks = chunk_text(
            clean,
            max_tokens=pipeline_config.chunk_tokens,
            overlap_tokens=pipeline_config.chunk_overlap,
        )
        print(f"[dry-run] {parsed.source_filename}: {len(chunks)} chunks "
              f"(pages={parsed.page_count}, paragraphs={parsed.paragraph_count})")
        for c in chunks[:3]:
            preview = c.text[:120].replace("\n", " ")
            print(f"  #{c.index} (~{c.token_count} tok): {preview}…")
        return

    if args.owner_id is None:
        parser.error("--owner-id is required (the owning app_user.id) unless --dry-run")
    result = ingest(args.path, owner_id=args.owner_id, title=args.title, project=args.project)
    print(
        f"Ingested {result.source_filename!r}\n"
        f"  brd_id     : {result.brd_id}\n"
        f"  title      : {result.title}\n"
        f"  project    : {result.project or '-'}   (UI dropdown id)\n"
        f"  document_id: {result.document_id if result.document_id is not None else '-'}\n"
        f"  chunks     : {result.chunk_count}\n"
        f"  embed_dim  : {result.embed_dim}\n"
        f"  tokens     : {result.total_tokens}\n"
        f"  status     : {result.status}"
    )
    print(f"\n  -> Appears in the UI BRD selector as {result.project!r}. Query it:")
    print(f"     .venv\\Scripts\\python.exe -m backend.retrieve.retriever \"your question\" "
          f"--project {result.project}")


if __name__ == "__main__":
    main()

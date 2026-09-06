"""Turn parsed sections into retrieval-sized chunks.

Strategy is section-aware: each section becomes one chunk, keeping a single
requirement whole. A section longer than MAX_TOKENS is split on paragraph
boundaries with a small overlap so no requirement is silently cut in half.

Each chunk carries a SHA-256 content hash — the key that lets re-ingestion skip
unchanged chunks instead of re-embedding them.

    python -m backend.ingest.chunker data/brds/payments-brd.md
"""
from __future__ import annotations

import hashlib
import sys

from ..models import Chunk, ParsedSection
from .parser import parse

MAX_TOKENS = 400          # target ceiling for a single chunk
OVERLAP_TOKENS = 60       # carried between split pieces to preserve context


def _est_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token). Real usage is returned by Voyage."""
    return max(1, round(len(text) / 4))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snippet(text: str, n: int = 48) -> str:
    """A short, single-line label derived from a chunk's own text — used as the
    citation tag when a chunk has no requirement ID (e.g. heading-less PDFs), so
    each chunk is distinguishable instead of all sharing the document title."""
    line = " ".join(text.strip().split())
    return (line[:n] + "…") if len(line) > n else line


def _is_sep(line: str) -> bool:
    """A GitHub table separator row, e.g. `| --- | :---: |` — structural, not data."""
    body = line.replace("|", "").replace(":", "").strip()
    return bool(body) and set(body) <= {"-", " "}


def _looks_table(text: str) -> bool:
    """A block whose lines are mostly `|`-delimited rows — a Markdown table. Detected
    so each row becomes its own chunk instead of being fused into one blob (which is
    what made flattened tables un-editable). One-line or single-pipe text is not one."""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return False
    piped = sum(1 for l in lines if "|" in l)
    return piped >= 2 and piped >= len(lines) * 0.6


def _split_table(text: str) -> list[str]:
    """Fan a Markdown table into one piece per DATA row, each carrying the header row
    for column context. Keeps every requirement row atomic — individually retrievable
    and editable, and never word-shredded. Content is preserved (rows are verbatim)."""
    lines = [l for l in text.splitlines() if l.strip()]
    hi = next((i for i, l in enumerate(lines) if "|" in l), None)
    if hi is None:
        return [text]
    preamble, header, rest = lines[:hi], lines[hi], lines[hi + 1:]
    data = [l for l in rest if not _is_sep(l)]
    if not data:
        return [text]                              # header only — nothing to fan out
    prefix = "\n".join([*preamble, header])
    pieces: list[str] = []
    for row in data:
        piece = f"{prefix}\n{row}"
        pieces.extend([piece] if _est_tokens(piece) <= MAX_TOKENS else _window(piece))
    return pieces


def _split(text: str) -> list[str]:
    """Keep a section whole if it fits; otherwise pack paragraphs with overlap.
    A Markdown table is fanned into one chunk per row (see _split_table)."""
    if _looks_table(text):
        return _split_table(text)
    if _est_tokens(text) <= MAX_TOKENS:
        return [text]

    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    pieces: list[str] = []
    buf: list[str] = []
    for para in paras:
        buf.append(para)
        if _est_tokens("\n\n".join(buf)) >= MAX_TOKENS:
            pieces.append("\n\n".join(buf))
            # start the next piece with a tail overlap of the last paragraph
            buf = [para] if _est_tokens(para) <= OVERLAP_TOKENS else []
    if buf:
        pieces.append("\n\n".join(buf))
    # Fallback: hard-split any single piece still over the ceiling (e.g. a PDF page
    # with no paragraph breaks) into word-windows, so no chunk is oversized.
    out: list[str] = []
    for piece in pieces:
        if _est_tokens(piece) <= MAX_TOKENS:
            out.append(piece)
        else:
            out.extend(_window(piece))
    return out


def _window(text: str) -> list[str]:
    max_chars = MAX_TOKENS * 4
    words, chunks, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            chunks.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        chunks.append(cur)
    return chunks


def chunk_sections(sections: list[ParsedSection]) -> list[Chunk]:
    chunks: list[Chunk] = []
    ordinal = 0
    for s in sections:
        for piece in _split(s.text):
            chunks.append(
                Chunk(
                    ordinal=ordinal,
                    section=s.section if s.req_id else _snippet(piece),
                    req_id=s.req_id,
                    text=piece,
                    token_count=_est_tokens(piece),
                    content_hash=_hash(piece),
                )
            )
            ordinal += 1
    return chunks


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m backend.ingest.chunker <path-to-brd.md>")
    _, sections = parse(sys.argv[1])
    chunks = chunk_sections(sections)
    print(f"chunks : {len(chunks)}\n")
    print(f"{'#':>2}  {'req_id':<7}  {'tok':>4}  {'hash':<12}  preview")
    print("-" * 88)
    for c in chunks:
        preview = c.text.replace("\n", " ")[:52]
        print(f"{c.ordinal:>2}  {c.req_id or '-':<7}  {c.token_count:>4}  {c.content_hash[:12]}  {preview}")

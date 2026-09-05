"""Stage 3 — Chunking. Pure, no DB side effects.

Split cleaned text into ~500-token segments with ~50-token overlap, packing
whole sentences/clauses so a functional requirement is never cut mid-clause.

Token counting is INJECTED as `count_tokens: Callable[[str], int]` so the chunk
sizes match whatever embedding model is in play (in production: the Voyage
tokenizer via `embedding.voyage_token_counter`; in tests: a trivial word
counter). This keeps the function pure and unit-testable with no network/model.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

TokenCounter = Callable[[str], int]

# Split into clause-sized units: sentence-ending punctuation or newlines.
_UNIT_SPLIT = re.compile(r"(?<=[.!?;:])\s+|\n+")
_WORD = re.compile(r"\S+")


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    token_count: int


def approx_token_counter(text: str) -> int:
    """Cheap, deterministic word-count proxy for tests / offline fallback."""
    return len(_WORD.findall(text))


def _split_units(text: str) -> list[str]:
    """Break text into ordered clause/sentence units, respecting paragraphs."""
    units: list[str] = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        for part in _UNIT_SPLIT.split(para):
            part = part.strip()
            if part:
                units.append(part)
    return units


def _hard_split(unit: str, max_tokens: int, count_tokens: TokenCounter) -> list[str]:
    """Fallback for a single unit larger than max_tokens: window over words."""
    words = unit.split()
    pieces: list[str] = []
    current: list[str] = []
    for word in words:
        current.append(word)
        if count_tokens(" ".join(current)) >= max_tokens:
            pieces.append(" ".join(current))
            current = []
    if current:
        pieces.append(" ".join(current))
    return pieces or [unit]


def chunk_text(
    text: str,
    *,
    max_tokens: int = 500,
    overlap_tokens: int = 50,
    count_tokens: TokenCounter = approx_token_counter,
) -> list[Chunk]:
    """Greedy token-budgeted packing with backward overlap between chunks."""
    if not text or not text.strip():
        return []
    if overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens must be smaller than max_tokens")

    # Pre-size units, hard-splitting any that individually blow the budget.
    sized: list[tuple[str, int]] = []
    for unit in _split_units(text):
        tokens = count_tokens(unit)
        if tokens <= max_tokens:
            sized.append((unit, tokens))
        else:
            for piece in _hard_split(unit, max_tokens, count_tokens):
                sized.append((piece, count_tokens(piece)))

    chunks: list[Chunk] = []
    n = len(sized)
    i = 0
    idx = 0
    while i < n:
        current: list[str] = []
        current_tokens = 0
        j = i
        # Pack until the next unit would exceed the budget (always take >=1 unit).
        while j < n and (not current or current_tokens + sized[j][1] <= max_tokens):
            current.append(sized[j][0])
            current_tokens += sized[j][1]
            j += 1
        chunks.append(Chunk(index=idx, text=" ".join(current).strip(), token_count=current_tokens))
        idx += 1
        if j >= n:
            break
        # Step back from j to include ~overlap_tokens of trailing context next time.
        back_tokens = 0
        k = j
        while k > i and back_tokens < overlap_tokens:
            k -= 1
            back_tokens += sized[k][1]
        i = max(k, i + 1)  # guarantee forward progress

    logger.info("chunk_text: %d units -> %d chunks (max=%d, overlap=%d)",
                n, len(chunks), max_tokens, overlap_tokens)
    return chunks

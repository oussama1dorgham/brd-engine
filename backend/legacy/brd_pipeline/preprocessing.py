"""Stage 2 — Preprocessing & metadata. Pure, no DB side effects.

Normalize whitespace and strip control characters WITHOUT destroying paragraph
breaks (a blank line between blocks is meaningful business-rule structure).
Derive a human title from the filename (overridable) and mint a stable brd_id.
"""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Control chars to strip, but KEEP \n (0x0A) and \t (0x09) as structure.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HORIZONTAL_WS = re.compile(r"[ \t]+")           # runs of spaces/tabs -> one space
_TRAILING_WS = re.compile(r"[ \t]+(\n)")          # spaces before a newline
_LEADING_WS = re.compile(r"(\n)[ \t]+")           # spaces after a newline
_BLANK_LINES = re.compile(r"\n{3,}")              # 3+ newlines -> paragraph break (\n\n)


def normalize_text(raw: str) -> str:
    """Collapse whitespace runs and remove control chars, preserving paragraphs."""
    if not raw:
        return ""
    text = _CONTROL_CHARS.sub("", raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HORIZONTAL_WS.sub(" ", text)
    text = _TRAILING_WS.sub(r"\1", text)
    text = _LEADING_WS.sub(r"\1", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def derive_title(source_filename: str, *, override: str | None = None) -> str:
    """Human-readable BRD title from the filename, unless an override is given."""
    if override and override.strip():
        return override.strip()
    stem = Path(source_filename).stem
    pretty = re.sub(r"[_\-]+", " ", stem).strip()
    pretty = re.sub(r"\s+", " ", pretty)
    title = pretty.title() if pretty else source_filename
    logger.debug("derive_title: %s -> %r", source_filename, title)
    return title


def new_brd_id() -> str:
    """Mint a stable, unique BRD id (UUID4 as string)."""
    return str(uuid.uuid4())

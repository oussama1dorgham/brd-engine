"""Shared data structures passed between the ingestion stages.

parse  -> DocumentMeta + list[ParsedSection]
chunk  -> list[Chunk]
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DocumentMeta:
    """Identity of a BRD, taken from its front matter."""
    title: str
    project: str
    version: str = "v1"
    status: str = "draft"          # draft | approved | superseded


@dataclass
class ParsedSection:
    """A structurally whole piece of a BRD (usually one heading's body)."""
    ordinal: int                   # order within the document
    title: str                     # the heading text, e.g. "FR-14 — Authentication"
    section: str                   # parent level-2 section, e.g. "3. Functional Requirements"
    req_id: str | None             # e.g. "FR-14" if the block declares one
    text: str                      # the body text under the heading


@dataclass
class Chunk:
    """A retrieval unit: what gets embedded and stored."""
    ordinal: int
    section: str
    req_id: str | None
    text: str
    token_count: int
    content_hash: str              # sha256 of text; lets us skip re-embedding

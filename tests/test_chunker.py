"""Chunker: section-aware splitting, hashing, ordinals."""
from backend.ingest.chunker import (
    MAX_TOKENS, _hash, _looks_table, _split, _split_table, chunk_sections,
)
from backend.models import ParsedSection


def test_short_section_is_one_chunk():
    secs = [ParsedSection(0, "FR-1 — Login", "Functional", "FR-1", "Users must log in via SSO.")]
    chunks = chunk_sections(secs)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.req_id == "FR-1"
    assert c.content_hash == _hash("Users must log in via SSO.")
    assert c.token_count >= 1


def test_hash_is_deterministic_and_sensitive():
    assert _hash("abc") == _hash("abc")
    assert _hash("abc") != _hash("abd")


def test_long_section_splits():
    para = "word " * 300              # ~375 tokens each
    long_text = ((para.strip() + "\n\n") * 3).strip()
    assert len(_split(long_text)) > 1  # exceeds MAX_TOKENS -> multiple pieces


def test_short_text_not_split():
    assert _split("just a short requirement") == ["just a short requirement"]
    assert MAX_TOKENS > 0


def test_table_fans_into_one_chunk_per_row():
    table = ("| Role | Responsibilities |\n"
             "| --- | --- |\n"
             "| Admin | creates settings, weekly reports |\n"
             "| Manager | manages the portfolio |\n"
             "| Officer | coordinates and delegates |")
    assert _looks_table(table)
    pieces = _split_table(table)
    assert len(pieces) == 3                       # one per DATA row (separator dropped)
    # every row carries the header for column context, and its own data verbatim
    assert all(p.startswith("| Role | Responsibilities |") for p in pieces)
    assert "Admin | creates settings" in pieces[0]
    assert "Officer | coordinates and delegates" in pieces[2]
    # no row is the separator line
    assert not any("---" in p.splitlines()[-1] for p in pieces)


def test_prose_is_not_treated_as_table():
    assert not _looks_table("A single prose requirement with no pipes at all.")
    assert not _looks_table("Only | one pipe on one line.")


def test_ordinals_are_sequential():
    secs = [
        ParsedSection(0, "A", "S", None, "text a"),
        ParsedSection(1, "B", "S", "FR-2", "text b"),
    ]
    chunks = chunk_sections(secs)
    assert [c.ordinal for c in chunks] == [0, 1]

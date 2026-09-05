"""Chunker: section-aware splitting, hashing, ordinals."""
from backend.ingest.chunker import MAX_TOKENS, _hash, _split, chunk_sections
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


def test_ordinals_are_sequential():
    secs = [
        ParsedSection(0, "A", "S", None, "text a"),
        ParsedSection(1, "B", "S", "FR-2", "text b"),
    ]
    chunks = chunk_sections(secs)
    assert [c.ordinal for c in chunks] == [0, 1]

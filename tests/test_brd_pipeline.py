"""Unit tests for the pure stages of the BRD pipeline (no network, no DB).

Run:  .venv\\Scripts\\python.exe -m pytest tests/test_brd_pipeline.py -q
"""
from __future__ import annotations

import io

import pytest

from backend.brd_pipeline.chunking import approx_token_counter, chunk_text
from backend.brd_pipeline.parsing import UnsupportedFileTypeError, parse_document
from backend.brd_pipeline.preprocessing import derive_title, new_brd_id, normalize_text


# --- preprocessing ---------------------------------------------------------

def test_normalize_collapses_spaces_but_keeps_paragraphs():
    raw = "Rule  1:   the\tsystem\n\n\n\nshall   log.\x07"
    out = normalize_text(raw)
    assert "  " not in out
    assert "\x07" not in out
    assert "\n\n" in out            # paragraph break preserved
    assert "\n\n\n" not in out      # but collapsed to a single blank line


def test_normalize_strips_control_chars_keeps_newlines_tabs():
    assert normalize_text("a\x00b") == "a b" or normalize_text("a\x00b") == "ab"
    assert "\n" in normalize_text("line1\nline2")


def test_derive_title_from_filename_and_override():
    assert derive_title("payments_brd-v2.docx") == "Payments Brd V2"
    assert derive_title("anything.pdf", override="Custom Title") == "Custom Title"


def test_new_brd_id_is_unique_uuid():
    a, b = new_brd_id(), new_brd_id()
    assert a != b and len(a) == 36


# --- chunking --------------------------------------------------------------

def test_chunk_respects_token_budget_and_overlaps():
    text = ". ".join(f"requirement number {i} the system shall do thing {i}" for i in range(40))
    chunks = chunk_text(text, max_tokens=30, overlap_tokens=8, count_tokens=approx_token_counter)
    assert len(chunks) > 1
    for c in chunks:
        assert c.token_count <= 30
    # consecutive chunks share trailing/leading context (overlap)
    first_tail = set(chunks[0].text.split()[-5:])
    second_head = set(chunks[1].text.split()[:8])
    assert first_tail & second_head


def test_chunk_empty_text_returns_no_chunks():
    assert chunk_text("   \n\n  ") == []


def test_chunk_overlap_must_be_smaller_than_max():
    with pytest.raises(ValueError):
        chunk_text("hello world", max_tokens=5, overlap_tokens=5)


def test_oversized_unit_is_hard_split():
    giant = "word " * 200  # one "unit" far bigger than the budget
    chunks = chunk_text(giant, max_tokens=20, overlap_tokens=5)
    assert chunks and all(c.token_count <= 20 for c in chunks)


# --- parsing dispatch ------------------------------------------------------

def test_parse_document_rejects_unsupported_type():
    with pytest.raises(UnsupportedFileTypeError):
        parse_document("notes.txt")


def test_parse_document_filelike_without_name_raises():
    with pytest.raises(UnsupportedFileTypeError):
        parse_document(io.BytesIO(b"%PDF-1.4"))  # no filename -> cannot detect type

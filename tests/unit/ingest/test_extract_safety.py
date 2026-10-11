"""Safety hardening for backend.ingest.extract:
  * the zip-bomb size/ratio guard applies to EVERY member read (not just word/document.xml)
  * a malformed document becomes a non-retryable ExtractError, never a bare parser exception
  * w:sdt / w:customXml wrappers (content controls, repeating sections) are walked, not skipped
  * tracked-move SOURCE text (w:moveFrom) is not duplicated alongside its destination (w:moveTo)
  * the Arabic visual-order quality gate fires on a meaningful Arabic WORD COUNT, not only when
    Arabic is the majority of the whole text

Pure unit tests: DOCX bytes are built in memory with zipfile + minimal OOXML strings, no DB or
network.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from backend.ingest import extract

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _make_docx(document_xml: str, styles_xml: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", document_xml)
        if styles_xml is not None:
            z.writestr("word/styles.xml", styles_xml)
    return buf.getvalue()


def _doc(body: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><w:document {NS}><w:body>{body}</w:body></w:document>'


SMALL_DOC = _doc("<w:p><w:r><w:t>Hello world</w:t></w:r></w:p>")


# --- (a) the zip-bomb guard covers every member, not just document.xml -----------------------

def test_huge_styles_xml_rejected_even_with_small_document_xml():
    huge_styles = " " * (60 * 1024 * 1024)        # highly compressible -> compresses in well under a second
    data = _make_docx(SMALL_DOC, styles_xml=huge_styles)
    with pytest.raises(extract.ExtractError, match="unsafe size"):
        extract.extract(data, "bomb.docx")


# --- (b) malformed documents raise ExtractError, never a bare parser exception ----------------

def test_malformed_document_xml_raises_extract_error_not_parse_error():
    data = _make_docx("<w:document><w:body><w:p>unterminated")
    with pytest.raises(extract.ExtractError):
        extract.extract(data, "broken.docx")


def test_corrupt_deflate_member_raises_extract_error_not_zlib_error():
    """A zip member whose compressed bytes are corrupt raises zlib.error deep inside
    zipfile's decompressor when read — not a RuntimeError/ValueError/KeyError, so it must
    be caught by the broad fallback, not escape as a retryable bare exception."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", SMALL_DOC)
    data = bytearray(buf.getvalue())
    doc_offset = data.find(b"word/document.xml")
    # corrupt a run of bytes well after the local-file-header's filename (into the deflate
    # stream itself) so the member size/ratio guard still passes but decompression fails.
    for off in range(doc_offset + 80, min(doc_offset + 120, len(data))):
        data[off] = 0xFF
    with pytest.raises(extract.ExtractError):
        extract.extract(bytes(data), "corrupt.docx")


def test_unknown_xml_encoding_raises_extract_error_not_lookup_error():
    """An XML prolog declaring an encoding ElementTree doesn't recognize raises LookupError
    from inside ET.fromstring — not one of the previously-caught exception types."""
    bad_doc = (
        f'<?xml version="1.0" encoding="totally-not-a-real-encoding"?>'
        f'<w:document {NS}><w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body></w:document>'
    ).encode("utf-8")
    data = _make_docx(bad_doc.decode("utf-8"))
    with pytest.raises(extract.ExtractError):
        extract.extract(data, "badencoding.docx")


# --- (c) w:sdt (content control) and w:customXml wrappers are walked, not skipped -------------

def test_sdt_table_row_and_customxml_paragraph_are_kept():
    body = (
        "<w:customXml><w:p><w:r><w:t>Custom paragraph text</w:t></w:r></w:p></w:customXml>"
        "<w:tbl>"
        "<w:tr><w:tc><w:p><w:r><w:t>H1</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>H2</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:sdt><w:sdtContent>"
        "<w:tr><w:tc><w:p><w:r><w:t>R1C1</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>R1C2</w:t></w:r></w:p></w:tc></w:tr>"
        "</w:sdtContent></w:sdt>"
        "</w:tbl>"
    )
    data = _make_docx(_doc(body))
    ex = extract.extract(data, "sdt.docx")
    assert "Custom paragraph text" in ex.markdown
    assert "R1C1" in ex.markdown and "R1C2" in ex.markdown


# --- (d) w:moveFrom (tracked-move source) text is not duplicated -------------------------------

def test_move_from_text_not_duplicated():
    body = (
        "<w:p><w:moveFrom><w:r><w:t>MovedParagraphText</w:t></w:r></w:moveFrom></w:p>"
        "<w:p><w:moveTo><w:r><w:t>MovedParagraphText</w:t></w:r></w:moveTo></w:p>"
    )
    data = _make_docx(_doc(body))
    ex = extract.extract(data, "move.docx")
    assert ex.markdown.count("MovedParagraphText") == 1


# --- (e) the RTL quality gate fires on word count, not only a text-wide Arabic percentage -----

def test_rtl_gate_fires_for_mostly_english_text_with_50_reversed_arabic_words():
    english = " ".join(["This is an ordinary English requirement sentence about the system."] * 80)
    reversed_arabic = " ".join(["كتابلا"] * 50)   # each word matches the visual-order artefact pattern
    body = (
        f"<w:p><w:r><w:t>{english}</w:t></w:r></w:p>"
        f"<w:p><w:r><w:t>{reversed_arabic}</w:t></w:r></w:p>"
    )
    data = _make_docx(_doc(body))
    # sanity: this text is NOT majority-Arabic, so the OLD (>30%-of-text) gate would have missed it
    assert not extract._is_rtl(english + " " + reversed_arabic)
    with pytest.raises(extract.ExtractError, match="reversed"):
        extract.extract(data, "mixed.docx")


# --- (f) the PyMuPDF -> pypdf retry is gated on which path ACTUALLY ran, not the aggregate
#         Arabic ratio of the result ------------------------------------------------------

def test_rtl_gate_retries_pypdf_path_even_when_pymupdf_output_is_majority_arabic(monkeypatch):
    """Regression: the retry used to be gated on `not _is_rtl(out.markdown)` — "retry only if
    the PyMuPDF result ISN'T majority Arabic" — assuming a majority-Arabic result already went
    through _pdf_rtl (since _pdf() picks a path from only the FIRST 15 pages). An English
    cover/TOC with a large reversed-Arabic body breaks that assumption: PyMuPDF ran (the sampled
    pages were English) but the overall output still ends up majority-Arabic, so the old check
    wrongly skipped the retry. The fix tracks which path actually produced the result
    (`used_rtl_path`), not the aggregate Arabic ratio of the text."""
    reversed_arabic = " ".join(["كتابلا"] * 80)   # majority Arabic AND visual-order (reversed)
    bad = extract.Extracted(markdown=reversed_arabic, file_type="pdf", page_count=5, used_rtl_path=False)
    good_arabic = " ".join(["كتاب"] * 80)         # majority Arabic, correct (logical) order
    good = extract.Extracted(markdown=good_arabic, file_type="pdf", page_count=5, used_rtl_path=True)

    # sanity: the old gate condition (`not _is_rtl(...)`) would have been False here, skipping retry
    assert extract._is_rtl(reversed_arabic)

    monkeypatch.setattr(extract, "_pdf", lambda data: bad)
    monkeypatch.setattr(extract, "_pdf_rtl", lambda data, page_count: good)
    result = extract.extract(b"%PDF-1.4 fake", "doc.pdf")
    assert result.markdown == good_arabic
    assert result.used_rtl_path is True

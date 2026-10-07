"""Regression: Arabic (right-to-left) PDFs must be extracted in LOGICAL order.

PyMuPDF returns RTL text in the PDF's visual order — table cells came out with every character
reversed ("ي فيرعتلا مقرلا" for "الرقم التعريفي") and prose with swapped lam-alef pairs
("االستخدام"), which made a whole BRD unanswerable. RTL PDFs now go through pypdf. This runs
on every Arabic PDF source already in object storage (skips when there is none)."""
import re

import pytest

from backend.core import storage
from backend.core.db import pool
from backend.ingest import chunker, extract, parser

pytestmark = pytest.mark.integration

# visual-order artefacts: reversed "ال" article at a word end / lam-alef swapped
REVERSED = re.compile(r"\b[ء-ي]+لا\b|\bاال[ء-ي]|\bامل[ء-ي]")


def _arabic_pdf_sources():
    with pool().connection() as c, c.cursor() as cur:
        cur.execute("select id, object_key, filename from brd_source where mime = 'application/pdf' order by id")
        return cur.fetchall()


def test_rtl_pdfs_extract_in_logical_order():
    try:
        sources = _arabic_pdf_sources()
        st = storage.store()
    except storage.StorageError as e:
        pytest.skip(f"object storage unavailable: {e}")
    checked = 0
    for sid, key, filename in sources:
        data = st.get(key)
        ex = extract.extract(data, filename)
        if not extract._is_rtl(ex.markdown):
            continue
        checked += 1
        words = re.findall(r"[ء-ي]+", ex.markdown)
        bad = [w for w in words if REVERSED.search(w)]
        assert len(bad) < 0.01 * len(words), f"source {sid}: {len(bad)} visual-order words, e.g. {bad[:5]}"
        chunks = chunker.build_chunks(parser.parse_text(ex.markdown, filename))
        assert chunks and len({c.section for c in chunks}) > 1, f"source {sid}: no section structure"
        # body lines starting with '#' (a table's "#" column) are escaped and must never become sections
        escaped = {l.strip().lstrip("#").strip() for l in ex.markdown.splitlines() if l.startswith(" #")}
        assert not any(c.section in escaped for c in chunks), "a body line leaked in as a heading"
    if not checked:
        pytest.skip("no Arabic PDF source in storage")

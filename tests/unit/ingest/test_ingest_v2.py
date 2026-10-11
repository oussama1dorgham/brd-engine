"""Pure-logic tests for ingestion v2: naming, upload validation, structure-aware parsing and
chunking, revision alignment, and the Voyage free-tier budget (no DB / network)."""
import io
import zipfile

import pytest

from backend.ingest import chunker, extract, parser, revise
from backend.ingest.jobs import derive_title, slugify
from backend.core.voyage import Budget


# --- naming -----------------------------------------------------------------------------

def test_slugify_keeps_every_script():
    assert slugify("Directives BRD") == "directives-brd"
    assert slugify("وثيقة متطلبات الأعمال") == "وثيقة-متطلبات-الأعمال"     # no longer collapses to 'brd'
    assert slugify("وثيقة متطلبات الأعمال") != slugify("متطلبات أخرى")
    assert slugify("  ***  ") == "brd"


def test_derive_title():
    assert derive_title("payments_portal-brd.docx") == "Payments portal brd"
    assert derive_title("x.pdf", "  My   BRD ") == "My BRD"


# --- upload validation (content, not extension) -----------------------------------------

def _zip(files: dict) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return b.getvalue()


def test_sniff_by_content():
    assert extract.sniff(b"%PDF-1.7 ...", "a.pdf") == "pdf"
    assert extract.sniff(_zip({"word/document.xml": "<w/>"}), "a.docx") == "docx"
    with pytest.raises(extract.ExtractError):
        extract.sniff(b"%PDF-1.7", "a.docx")                    # content/extension mismatch
    with pytest.raises(extract.ExtractError):
        extract.sniff(_zip({"xl/workbook.xml": "<x/>"}), "a.docx")   # a zip, but not Word
    with pytest.raises(extract.ExtractError):
        extract.sniff(b"MZ\x90\x00", "a.pdf")                  # an executable renamed .pdf


def test_sniff_rejects_zip_bomb():
    bomb = _zip({"word/document.xml": "a" * (5 * 1024 * 1024)})   # compresses ~1000x
    with pytest.raises(extract.ExtractError, match="unsafe"):
        extract.sniff(bomb, "a.docx")


def test_normalize_folds_presentation_forms():
    assert extract.normalize("ﻣﺮﺣﺒﺎ") == "مرحبا"                  # Arabic presentation forms -> letters
    assert extract.normalize("a​b  c") == "ab c"


# --- structure-aware parsing + chunking ---------------------------------------------------

MD = """<!-- page:3 -->
## 2. Functional Requirements

### 2.1 Authentication

FR-1 The system shall authenticate users.

FR 2 The system shall lock accounts.

Supported factors:

- email code
- authenticator app

### 2.2 Refunds

| ID | Requirement |
| --- | --- |
| FR-10 | Full refund of a settled payment. |
| NFR-3 | Status updates within 5 seconds. |
"""


def test_find_req_id_formats():
    assert parser.find_req_id("FR 2 The system") == "FR-2"
    assert parser.find_req_id("REQ-12.3 must") == "REQ-12.3"
    assert parser.find_req_id("the fr-2 lower case") is None
    assert parser.find_req_id("For 3 users") is None


def test_parse_text_path_and_page():
    secs = parser.parse_text(MD, "BRD")
    auth = next(s for s in secs if s.title == "2.1 Authentication")
    assert auth.section == "2. Functional Requirements"
    assert auth.section_path == "2. Functional Requirements / 2.1 Authentication"
    assert auth.page == 3


def test_build_chunks_one_requirement_each():
    chunks = chunker.build_chunks(parser.parse_text(MD, "BRD"))
    assert [c.req_id for c in chunks] == ["FR-1", "FR-2", "FR-10", "NFR-3"]
    fr2 = chunks[1]
    assert "Supported factors" in fr2.text and "authenticator app" in fr2.text   # lead-in + list stay with FR-2
    assert chunks[2].text.startswith("| ID | Requirement |")                     # table row keeps its header
    assert all(c.section == "2. Functional Requirements" for c in chunks)


def test_embed_input_carries_context_but_text_does_not():
    c = chunker.build_chunks(parser.parse_text(MD, "BRD"))[0]
    inp = chunker.embed_input("Payments BRD", c)
    assert inp.startswith("Payments BRD › 2. Functional Requirements / 2.1 Authentication")
    assert "Payments BRD" not in c.text
    assert chunker.embed_hash("m1", inp) != chunker.embed_hash("m2", inp)          # model is part of the key


# --- revision alignment -------------------------------------------------------------------

def _row(text, req_id=None, section="S"):
    return {"text": text, "content_hash": chunker._hash(text), "req_id": req_id, "section": section}


def test_align_classifies_changes():
    live = [_row("intro text here"), _row("FR-1 authenticate users by email", "FR-1"),
            _row("FR-2 lock after five attempts", "FR-2"), _row("NFR-3 status in five seconds", "NFR-3")]
    new = [_row("intro text here"), _row("FR-2 lock after three attempts and notify", "FR-2"),
           _row("FR-1 authenticate users by email", "FR-1"), _row("FR-12 second approver above limit", "FR-12")]
    items = revise.classify(live, new)
    kinds = {(i["change"], (i["new"] or i["live"]).get("req_id")) for i in items}
    assert kinds == {("same", None), ("modified", "FR-2"), ("same", "FR-1"), ("added", "FR-12"), ("removed", "NFR-3")}


def test_align_similarity_without_ids():
    live = [_row("The portal shall export monthly reports as CSV files for finance")]
    new = [_row("The portal shall export monthly reports as CSV and PDF files for finance")]
    items = revise.classify(live, new)
    assert items[0]["change"] == "modified" and items[0]["similarity"] > 0.6


def test_align_never_reuses_a_live_row():
    live = [_row("FR-1 same text", "FR-1")]
    new = [_row("FR-1 same text", "FR-1"), _row("FR-1 same text", "FR-1")]    # duplicated in the new file
    items = revise.classify(live, new)
    assert sorted(i["change"] for i in items) == ["added", "same"]


def test_similarity_arabic_normalization():
    assert revise.similarity("إدارة المحافظ", "ادارة المحافظ") == 1.0


# --- Voyage free-tier budget ---------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_budget_rpm_and_tpm():
    c = Clock()
    b = Budget(rpm=3, tpm=10_000, query_reserve=1, clock=c)
    for _ in range(3):
        assert b._admissible("query", 100, c())
        b.calls.append((c(), 100))
    assert not b._admissible("query", 100, c())            # RPM exhausted
    c.t += 61
    b._prune(c())
    assert b._admissible("query", 100, c())
    b.calls.append((c(), 9_000))
    assert not b._admissible("ingest", 2_000, c())         # would exceed 10K TPM


def test_budget_ingest_yields_to_queries():
    c = Clock()
    b = Budget(rpm=3, tpm=10_000, query_reserve=1, clock=c)
    b.queries_waiting = 1
    assert not b._admissible("ingest", 10, c())            # a waiting query goes first
    b.queries_waiting = 0
    b.last_query = c()                                     # chat active recently -> keep a slot free
    b.calls.append((c(), 10))
    assert b._admissible("ingest", 10, c())                # 2nd slot ok (3 - 1 reserved)
    b.calls.append((c(), 10))
    assert not b._admissible("ingest", 10, c())            # the last slot is reserved for chat
    assert b._admissible("query", 10, c())


def test_budget_oversize_call_runs_alone_and_backoff_blocks():
    c = Clock()
    b = Budget(rpm=3, tpm=10_000, query_reserve=1, clock=c)
    assert b._admissible("query", 50_000, c())             # bigger than TPM: allowed when window empty
    b.back_off(30)
    assert not b._admissible("query", 1, c())
    c.t += 31
    assert b._admissible("query", 1, c())

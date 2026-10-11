"""Headless / flattened BRDs: structure recovery in extraction, parsing and chunking.

A BRD with no heading styles is ONE section. These pin that it still comes out as one chunk
per requirement with the right id, business rules without a story's id, tables fanned by row
with their caption, labels carried as context, no chunk cut mid-sentence, and nothing lost."""
import io
import zipfile
from xml.sax.saxutils import escape

from backend.ingest import chunker, extract, parser
from backend.ingest.chunker import MAX_TOKENS, _window, build_chunks

TITLE = "Companion Calendar"


def chunks_of(md: str):
    return build_chunks(parser.parse_text(md, TITLE))


def story(n: int, criteria: int = 8) -> str:
    items = " ".join(f"{k}. Rule {k} of story {n} applies to every meeting in the period without exception."
                     for k in range(1, criteria + 1))
    return (f"US-{n:02d}: Story Title {n} As a Scheduling Officer I want feature {n} "
            f"So that work {n} is done Acceptance Criteria {items}")


# --- flattened text (no line breaks at all) ------------------------------------------------

def test_flattened_text_is_cut_at_inline_requirement_starts():
    md = "Intro text for the document. " + " ".join(story(n) for n in range(1, 6))
    chunks = chunks_of(md)
    for n in range(1, 6):
        mine = [c for c in chunks if f"feature {n} " in c.text or f"of story {n} " in c.text]
        assert mine and all(c.req_id == f"US-{n:02d}" for c in mine)          # right id on every piece
        assert all(f"of story {m} " not in c.text for c in mine for m in range(1, 6) if m != n)  # no mixing
    assert all(c.token_count <= MAX_TOKENS for c in chunks)


def test_short_paragraph_mentioning_two_ids_is_not_split():
    md = "## Scope\n\nThis release covers US-01: login and US-02: logout for every user."
    assert len(chunks_of(md)) == 1


# --- headless DOCX-like markdown (paragraphs, no headings) ---------------------------------

HEADLESS = "\n\n".join([
    "Companion Calendar User Stories and Business Rules",
    "18 user stories covering platform integration and role-based access.",
    "Timing Preferences",
    "| Timing Preference | Definition |\n| --- | --- |\n| As Available | No exact date or time. |\n"
    "| Current Week | Within the calendar week. |",
    "Contents",
    "| Section | Coverage |\n| --- | --- |\n| 1. Platform Integration | US-01 to US-03 |\n| 2. Notes | US-04 to US-06 |",
    "US-01: Access the Companion Calendar by Role",
    "As a Companion Calendar user I want to use the functions available to my role So that I can work.",
    "Acceptance Criteria",
    *[f"{k}. Consultant, Chief of Staff, and Executive Office Manager can do thing {k} on visible meetings "
      f"permitted by the existing platform and its access rules." for k in range(1, 12)],
    "US-02: Use the Calendar within the Meeting Service",
    "As a Companion Calendar user I want to review meetings So that I can add insights.",
    "Shared Business Rules",
    "Live Data and Active Scope",
    "The Current Schedule uses live meeting data from the existing platform.",
    "Audit",
    "The Audit Log records create, update, delete, and execution events.",
])


def test_every_piece_of_a_long_story_keeps_its_id_and_label():
    us01 = [c for c in chunks_of(HEADLESS) if "thing" in c.text or "functions available" in c.text]
    assert len(us01) >= 2                                                  # long enough to split
    assert all(c.req_id == "US-01" for c in us01)
    assert all("US-01: Access the Companion Calendar by Role" in (c.section_path or "") for c in us01)


def test_acceptance_criteria_label_stays_with_its_story():
    c = next(c for c in chunks_of(HEADLESS) if "Acceptance Criteria" in c.text)
    assert c.req_id == "US-01"


def test_business_rules_after_the_last_story_get_no_story_id_but_their_label():
    rules = [c for c in chunks_of(HEADLESS) if "live meeting data" in c.text or "Audit Log records" in c.text]
    assert rules and all(c.req_id is None for c in rules)
    live = next(c for c in rules if "live meeting data" in c.text)
    assert live.section_path == "Shared Business Rules / Live Data and Active Scope"
    assert "Shared Business Rules" in live.text            # the parent label line is kept, not dropped
    assert next(c for c in rules if "Audit Log records" in c.text).section_path == "Audit"
    assert not any(c.text.strip() == "Shared Business Rules" for c in chunks_of(HEADLESS))  # no lone-label chunk


def test_tables_in_a_headless_document_fan_out_by_row_with_their_caption():
    rows = [c for c in chunks_of(HEADLESS) if c.section_path == "Timing Preferences"]
    assert len(rows) == 2 and all(c.text.startswith("Timing Preferences\n| Timing Preference") for c in rows)
    contents = [c for c in chunks_of(HEADLESS) if c.section_path == "Contents"]
    assert len(contents) == 2 and all(c.req_id is None for c in contents)   # "US-01 to US-03" is a reference


def test_table_row_takes_the_id_in_its_first_cell():
    md = "## Requirements\n\n| ID | Description |\n| --- | --- |\n| FR-1 | Login by SSO |\n| FR-2 | Logout |"
    assert [c.req_id for c in chunks_of(md)] == ["FR-1", "FR-2"]
    # an id column that is not first still works: the row's ONLY id is its id
    md = "## Requirements\n\n| Name | ID |\n| --- | --- |\n| Login by SSO | FR-1 |\n| Covers both | FR-1, FR-2 |"
    assert [c.req_id for c in chunks_of(md)] == ["FR-1", None]


def test_nothing_is_lost():
    blob = " ".join(" ".join(c.text.split()) for c in chunks_of(HEADLESS))
    for block in HEADLESS.split("\n\n"):
        for line in block.splitlines():
            if "---" not in line:
                assert " ".join(line.split()) in blob, line


def test_a_short_section_with_one_requirement_is_kept_whole():
    md = "## Login\n\nFR-1: The system shall let users log in by SSO.\n\nUsers without SSO use a password."
    chunks = chunks_of(md)
    assert len(chunks) == 1 and chunks[0].req_id == "FR-1"


# --- sentence-aware windowing ---------------------------------------------------------------

def test_window_never_cuts_mid_sentence_or_orphans_a_list_number():
    text = " ".join(f"{k}. Item number {k} is a complete sentence about the planning period rules." for k in range(1, 60))
    pieces = _window(text)
    assert len(pieces) > 1
    for p in pieces:
        assert p.rstrip().endswith(".") and len(p) <= MAX_TOKENS * 4
        assert not p.rstrip().endswith((" 1.", " 2.")) and p.split()[0].rstrip(".").isdigit()
    assert " ".join(pieces) == text                       # nothing lost or duplicated


# --- parser: requirement id of a section ----------------------------------------------------

def test_sub_heading_inherits_the_enclosing_requirement_id():
    md = "## US-01: Access by Role\n\nAs a user I want access.\n\n### Acceptance Criteria\n\n1. Roles apply."
    secs = parser.parse_text(md, TITLE)
    assert [s.req_id for s in secs] == ["US-01", "US-01"]


def test_an_id_mentioned_later_in_a_section_is_not_its_id():
    md = "## Contents\n\nThe document has six sections; section one covers stories US-01 to US-03 in detail."
    assert parser.parse_text(md, TITLE)[0].req_id is None
    assert parser.parse_text("## Login\n\nRequirement ID: FR-3. Users log in.", TITLE)[0].req_id == "FR-3"


# --- extraction ------------------------------------------------------------------------------

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _run(t, bold=False):
    return f'<w:r>{"<w:rPr><w:b/></w:rPr>" if bold else ""}<w:t xml:space="preserve">{escape(t)}</w:t></w:r>'


def _docx(body: str) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {NS}><w:body>{body}</w:body></w:document>')
    return b.getvalue()


def test_docx_soft_line_breaks_become_separate_blocks():
    para = "<w:p>" + "<w:r><w:br/></w:r>".join(_run(x) for x in (
        "US-01: Access by Role", "As a user I want access.", "1. Roles apply.")) + "</w:p>"
    md = extract.extract(_docx(para), "a.docx").markdown
    assert md.split("\n\n") == ["US-01: Access by Role", "As a user I want access.", "1. Roles apply."]


def test_docx_page_break_is_not_a_line_break():
    para = f'<w:p>{_run("Before")}<w:r><w:br w:type="page"/></w:r>{_run("after.")}</w:p>'
    assert extract.extract(_docx(para), "a.docx").markdown == "Before after."


def test_docx_bold_label_line_is_recognised_and_stored_without_markers():
    body = (f"<w:p>{_run('Shared Business Rules', bold=True)}</w:p>"
            f"<w:p>{_run('The Current Schedule uses live data. ' * 30)}</w:p>"
            f"<w:p>{_run('Audit', bold=True)}</w:p>"
            f"<w:p>{_run('The Audit Log records every event. ' * 30)}</w:p>")
    md = extract.extract(_docx(body), "a.docx").markdown
    assert md.startswith("**Shared Business Rules**")
    chunks = chunks_of(md)
    assert {c.section_path for c in chunks} == {"Shared Business Rules", "Audit"}
    assert not any("**" in c.text for c in chunks)                         # BRD's own words only


def test_docx_bold_sentence_is_not_a_label():
    body = f"<w:p>{_run('This whole paragraph is bold and ends like a sentence.', bold=True)}</w:p>"
    assert extract.extract(_docx(body), "a.docx").markdown == "This whole paragraph is bold and ends like a sentence."


def test_pdf_bold_acceptance_criteria_is_not_a_heading():
    import pymupdf as fitz
    doc = fitz.open()
    page = doc.new_page()
    y = 60
    for text, bold in (("US-01: Access by Role", True), ("As a user I want access to my meetings.", False),
                       ("Acceptance Criteria", True), ("1. Roles apply to every visible meeting.", False)):
        page.insert_text((50, y), text, fontsize=11, fontname="hebo" if bold else "helv")
        y += 22
    md = extract.extract(doc.tobytes(), "a.pdf").markdown
    assert "#### US-01: Access by Role" in md
    assert "**Acceptance Criteria**" in md and "# Acceptance Criteria" not in md
    secs = parser.parse_text(md, TITLE)
    assert [s.req_id for s in secs if "Roles apply" in s.text] == ["US-01"]


def test_sub_label_and_title_detection():
    assert chunker.is_sub_label("**Acceptance Criteria**") and chunker.is_sub_label("Acceptance Criteria:")
    assert chunker._title_like("Shared Business Rules") == "Shared Business Rules"
    assert chunker._title_like("1. Platform Integration, Access, and Calendar")
    assert chunker._title_like("**Audit**") == "Audit"
    assert chunker._title_like("Acceptance Criteria") is None
    assert chunker._title_like("The system shall log every event.") is None    # a sentence
    assert chunker._title_like("users can view meetings") is None              # not a label
    assert chunker._title_like("- List Item Here") is None
    assert chunker._title_like("الأدوار والصلاحيات") == "الأدوار والصلاحيات"       # Arabic heading line


# --- review findings: ids come from what a text IS, never from what it mentions -------------

LONG = " ".join(f"Sentence {k} explains the officer confirmation rule in full detail." for k in range(40))


def test_whole_section_mentioning_an_id_gets_no_id():
    assert [c.req_id for c in chunks_of("## Scope\n\nThis release covers US-01: login and logout.")] == [None]
    assert [c.req_id for c in chunks_of("## FR-3: Login\n\nLogin depends on FR-1 for SSO.")] == ["FR-3"]


def test_a_line_mentioning_another_story_stays_in_its_own_story():
    md = "\n\n".join(["US-01: Access by Role", "As a user I want access.", "US-02: Confirm",
                      "Same as US-01, the officer must confirm. " + LONG[:900], "More text. " + LONG[:900]])
    chunks = chunks_of(md)
    assert all(c.req_id == "US-02" for c in chunks if "Same as US-01" in c.text or "More text" in c.text)


def test_a_wrapped_line_starting_with_an_id_is_not_a_new_requirement():
    md = "\n\n".join(["US-01: Reports", "As a user I want the same rules as", "US-02 so that the reports match.",
                      LONG[:1500], "US-02: Second", "As a user I want x."])
    chunks = chunks_of(md)
    assert next(c for c in chunks if "so that the reports match" in c.text).req_id == "US-01"
    assert [c.req_id for c in chunks if c.text.startswith("US-02: Second")] == ["US-02"]


def test_no_chunk_is_only_a_label():
    for md in ("\n\n".join(["US-01: Access by Role", LONG + " " + LONG]),
               "\n\n".join(["Reference Data", "Timing Preferences",
                            "| Preference | Definition |\n| --- | --- |\n| Now | Today. |", LONG])):
        chunks = chunks_of(md)
        assert all(c.token_count > 12 for c in chunks), [c.text for c in chunks]


def test_a_list_of_names_under_a_label_stays_one_block():
    md = "\n\n".join(["User Roles", "Consultant", "Chief of Staff", "Executive Office Manager", LONG])
    first = chunks_of(md)[0]
    assert first.section_path == "User Roles"
    assert all(r in first.text for r in ("Consultant", "Chief of Staff", "Executive Office Manager"))


def test_a_list_after_a_colon_line_stays_inside_its_story():
    md = "\n\n".join(["US-01: Access by Role", "The roles are:", "Consultant", "Chief of Staff",
                      "Executive Office Manager", LONG[:1200], "US-02: Next story", "As a user I want more."])
    chunks = chunks_of(md)
    roles = next(c for c in chunks if "Chief of Staff" in c.text)
    assert roles.req_id == "US-01" and "Executive Office Manager" in roles.text


def test_a_label_inside_a_story_stays_with_that_story():
    md = "\n\n".join(["US-07: Calendar", "As a user I want a calendar.", "Business Rules", LONG[:1500],
                      "US-08: Next", "As a user I want next."])
    assert all(c.req_id == "US-07" for c in chunks_of(md) if "Sentence" in c.text)


def test_a_labelled_block_inside_a_heading_section_keeps_the_heading_id():
    md = "## US-07: Calendar view\n\n" + "\n\n".join([LONG, "Business Rules", LONG])
    chunks = chunks_of(md)
    assert {c.req_id for c in chunks} == {"US-07"}
    assert any(c.section_path == "US-07: Calendar view / Business Rules" for c in chunks)


def test_a_range_heading_is_a_group_not_an_id():
    md = "## Functional Requirements (FR-1 to FR-20)\n\n### General Notes\n\nNotes text here."
    assert [c.req_id for c in chunks_of(md)] == [None]


def test_a_group_label_prefixes_the_requirements_under_it():
    md = "\n\n".join(["Module A", "FR-1: Login", LONG[:1200], "FR-2: Logout", "Users log out.",
                      "Module B", "FR-3: Export", LONG[:1200]])
    paths = {c.req_id: c.section_path for c in chunks_of(md)}
    assert paths == {"FR-1": "Module A / FR-1: Login", "FR-2": "Module A / FR-2: Logout",
                     "FR-3": "Module B / FR-3: Export"}


def test_a_key_value_table_takes_its_heading_id_over_a_mentioned_one():
    md = ("## UC-A05 - Settings\n\n| Field | Value |\n| --- | --- |\n| Identifier | UC-A05 |\n"
          "| Outputs | Escalation rules (see UC-E01) |")
    assert {c.req_id for c in chunks_of(md)} == {"UC-A05"}


def test_a_size_marked_heading_named_like_a_sub_label_stays_a_heading():
    import pymupdf as fitz
    doc = fitz.open()
    page = doc.new_page()
    y = 60
    for text, size in (("FR-20: Export", 18), ("Users can export the report as PDF.", 11),
                       ("Dependencies", 18), ("The HR feed must be available nightly.", 11),
                       ("FR-21: Import", 18), ("Users can import a CSV file of meetings.", 11)):
        page.insert_text((50, y), text, fontsize=size, fontname="helv")
        y += 30
    for p in range(2):                     # sizes must recur on 3+ pages to count as structural
        pg = doc.new_page()
        pg.insert_text((50, 60), f"FR-3{p}: Extra", fontsize=18, fontname="helv")
        pg.insert_text((50, 90), "Body text of the extra requirement.", fontsize=11, fontname="helv")
    md = extract.extract(doc.tobytes(), "a.pdf").markdown
    assert "# Dependencies" in md
    dep = next(c for c in chunks_of(md) if "HR feed" in c.text)
    assert dep.req_id != "FR-20"


def test_sentence_splitting_is_linear_on_long_numbered_runs():
    import time
    text = " ".join(f"Section {k % 100}." for k in range(12000))      # ~100 KB, every part merges
    t = time.perf_counter()
    pieces = _window(text)
    assert time.perf_counter() - t < 1.5 and " ".join(pieces).split() == text.split()


# --- pieces made outside ingestion, and what retrieval / the model see ----------------------

def test_a_split_row_is_cited_as_the_requirement_it_declares():
    from backend.ingest.chunker import piece_req_id
    assert piece_req_id("| ID | Desc |\n| FR-2 | Logout |", "FR-1") == "FR-2"
    assert piece_req_id("| Login by SSO | Users log in |", "FR-1") == "FR-1"
    assert piece_req_id("US-03: Export the report", "US-01") == "US-03"
    assert piece_req_id("Same as US-01, the officer confirms.", "US-02") == "US-02"


def test_a_requirement_added_next_to_another_is_not_embedded_as_part_of_it():
    from backend.ingest.chunker import container_path
    assert container_path("Module A / FR-1: Login", "FR-1") == "Module A"
    assert container_path("US-01: Access by Role", "US-01") is None
    assert container_path("Shared Business Rules / Audit", None) == "Shared Business Rules / Audit"
    assert container_path("Shared Business Rules / Audit", "FR-1") == "Shared Business Rules / Audit"


def test_reranker_and_model_see_the_label():
    from backend.chat.answer import LIST_QUESTION, _sources_block
    from backend.retrieve.retriever import _rerank_doc
    assert _rerank_doc((1, "US-01", "T", "p", "5. The officer confirms.", "US-01: Access by Role")) \
        == "US-01: Access by Role\n5. The officer confirms."
    assert _rerank_doc((1, "US-01", "T", "p", "US-01: Access by Role As a user…", "US-01: Access by Role")) \
        == "US-01: Access by Role As a user…"                          # already leads with it
    block = _sources_block([{"req_id": None, "section": "Doc", "section_path": "Shared Business Rules / Audit",
                             "project": "p", "text": "The Audit Log records events."}])
    assert block.startswith("[1] (Doc · Shared Business Rules / Audit · project=p)")
    assert LIST_QUESTION.search("what are the roles in the BRD?") and LIST_QUESTION.search("ما هي الأدوار؟")
    assert not LIST_QUESTION.search("Does the officer need a planning period?")


# --- review round 3 --------------------------------------------------------------------------

def test_an_overview_listing_stories_does_not_steal_their_ids():
    for overview in (["US-01: Access the calendar", "US-02: Create an event"],
                     ["- US-01: Access the calendar\n- US-02: Create an event"]):
        md = "\n\n".join(["Overview", *overview, "US-01: Access the Companion Calendar by Role",
                          "As a user I want access. " + LONG[:900], "US-02: Create an Event",
                          "As a user I want events. " + LONG[:900]])
        chunks = chunks_of(md)
        assert next(c for c in chunks if "Overview" in c.text).req_id is None
        assert next(c for c in chunks if "I want access" in c.text).req_id == "US-01"
        assert next(c for c in chunks if "I want events" in c.text).req_id == "US-02"


def test_a_module_label_between_stories_is_not_absorbed_into_the_previous_one():
    md = "\n\n".join(["US-07: Calendar", "As a user I want a calendar. " + LONG[:900], "Reporting Module",
                      "This module covers all reports produced by the platform.", "US-08: Reports",
                      "As a manager I want reports. " + LONG[:900]])
    intro = next(c for c in chunks_of(md) if "This module covers" in c.text)
    assert intro.req_id is None and intro.section_path == "Reporting Module"


def test_a_sentence_listing_ids_does_not_open_a_requirement():
    md = "## FR-3: Login\n\nThe system shall authenticate users with SSO.\n\nFR-4, FR-5 and FR-6 depend on this login flow."
    assert [c.req_id for c in chunks_of(md)] == ["FR-3"]


def test_only_real_list_questions_widen_retrieval():
    from backend.chat.answer import LIST_QUESTION
    for q in ("Can all users edit events?", "Does each request need approval?", "ما هو الحد الأقصى للمبلغ؟"):
        assert not LIST_QUESTION.search(q), q
    for q in ("list the roles", "what are the roles in the BRD?", "how many stories are there?", "ما هي الأدوار؟"):
        assert LIST_QUESTION.search(q), q

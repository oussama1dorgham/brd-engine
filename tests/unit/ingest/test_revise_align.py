"""Pure-logic tests for backend/ingest/revise.py's alignment (no DB / network):

  - table rows that happen to share a table header must be compared on their OWN content,
    not the shared header (otherwise unrelated rows of the same table get paired as
    "modified" and a use case inherits the wrong requirement's id — see docs/INGESTION.md §2).
  - a genuinely edited row must still be found as "modified".
  - alignment must stay fast at the ingest ceiling (INGEST_MAX_CHUNKS=2500; this test checks
    600×600, the size that measured ~35s under the old O(N×M) implementation).
  - the phase-3 prefilter must not silently drop a row whose shared words are all too common
    to pass the bucket cap — it must still be found, matching exhaustive Jaccard exactly.
  - _row_text's table-row detection must be structural, not chunker._looks_table's "60% of
    lines are piped" heuristic, which a multi-paragraph preamble can push under threshold.
"""
from __future__ import annotations

import hashlib
import itertools
import random
import time

from backend.ingest import chunker, parser, revise

_HEADER = "| ID | Requirement |\n| --- | --- |"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _table_rows(md_table_section: str) -> list[dict]:
    """Build real chunker output (one chunk per data row, header attached) so the test
    exercises the exact shape _row_text()/align() must handle, not a hand-rolled stand-in."""
    secs = parser.parse_text(md_table_section, "BRD")
    chunks = chunker.build_chunks(secs)
    return [{"text": c.text, "content_hash": c.content_hash, "req_id": c.req_id, "section": c.section}
            for c in chunks]


# --- table-row pairing must ignore the shared header -------------------------------------

_UNRELATED_TABLE = """## Scope

Preamble note: the following rows describe system behaviour for reporting and accounts in detail.

| ID | Requirement |
| --- | --- |
| FR-10 | Export monthly report data as CSV files for finance review. |
| FR-11 | Lock the customer account after three failed login attempts. |
"""


def test_unrelated_table_rows_score_below_threshold_on_row_content():
    """Sanity check on the reproduction: the OLD (header-included) Jaccard score crosses
    _SIM_MIN for these two genuinely unrelated rows, but the row-only score does not."""
    rows = _table_rows(_UNRELATED_TABLE)
    assert len(rows) == 2
    old_style = revise.similarity(rows[0]["text"], rows[1]["text"])
    row_only = revise.similarity(revise._row_text(rows[0]["text"]), revise._row_text(rows[1]["text"]))
    assert old_style >= revise._SIM_MIN, "fixture no longer reproduces the header-inflation bug"
    assert row_only < revise._SIM_MIN


def test_unrelated_table_rows_not_paired_as_modified():
    """The real bug: align()/classify() must not pair these rows just because they share
    the table's header — doing so would make the new row inherit the old row's requirement
    id, pointing any use case citing it at the wrong requirement."""
    rows = _table_rows(_UNRELATED_TABLE)
    live = [rows[0]]
    new = [rows[1]]
    items = revise.classify(live, new)
    kinds = {it["change"] for it in items}
    assert kinds == {"added", "removed"}
    assert not any(it["change"] == "modified" for it in items)


_EDITED_TABLE_BEFORE = """## Scope

Preamble note: a short note about refunds for card payments in the payments domain.

| ID | Requirement |
| --- | --- |
| FR-20 | The customer can request a refund of a settled card payment within 30 days. |
"""

_EDITED_TABLE_AFTER = """## Scope

Preamble note: a short note about refunds for card payments in the payments domain.

| ID | Requirement |
| --- | --- |
| FR-20 | The customer can request a refund of a settled card payment within 60 days. |
"""


def test_genuinely_edited_table_row_is_still_modified():
    """The fix must not overcorrect: a real edit to the same row (same header, same id,
    one changed detail) must still align as 'modified', not get thrown out with the header
    stripping."""
    live = _table_rows(_EDITED_TABLE_BEFORE)
    new = _table_rows(_EDITED_TABLE_AFTER)
    items = revise.classify(live, new)
    assert len(items) == 1
    assert items[0]["change"] == "modified"
    assert items[0]["similarity"] > 0.6


# --- the section bonus alone must never cross the pairing threshold ----------------------

def test_section_bonus_cannot_carry_a_pair_over_threshold():
    """Two rows whose ROW-ONLY similarity sits just under _SIM_MIN, in the same section
    (so the +0.05 bonus would apply), must still not be paired — the bonus may only order
    candidates, never decide membership on its own."""
    rows = _table_rows(_UNRELATED_TABLE)
    # both already share a section ("Scope"); row-only similarity is well under the
    # threshold (checked above), so even with the bonus they must not be paired.
    live, new = [rows[0]], [rows[1]]
    assert live[0]["section"] == new[0]["section"]
    pairs = revise.align(live, new)
    assert pairs == {}


# --- performance: near-linear, not O(live × new) ------------------------------------------

def _random_table_row(idx: int, words: list[str]) -> dict:
    row = f"| R-{idx} | {' '.join(words)} |"
    text = f"{_HEADER}\n{row}"
    return {"text": text, "content_hash": _hash(text), "req_id": None, "section": "Scope"}


def test_align_600x600_under_3_seconds_and_finds_the_real_edit():
    """600×600 table-row chunks with no shared ids forces the full text-similarity path
    (phases 1 and 2 of align() can't help). The old O(N×M), re-tokenize-per-pair
    implementation measured ~35 s here; this must stay well under 3 s. One genuine edit
    (11 of 12 words kept) is planted and must still be found as the best match for its row."""
    rng = random.Random(20240522)
    vocab = [f"term{i}" for i in range(3000)]
    n = 600
    live_words = [rng.sample(vocab, 12) for _ in range(n)]
    new_words = [rng.sample(vocab, 12) for _ in range(n)]
    live = [_random_table_row(i, live_words[i]) for i in range(n)]
    new = [_random_table_row(i, new_words[i]) for i in range(n)]

    edited_idx = 300
    new[edited_idx] = _random_table_row(edited_idx, live_words[edited_idx][:-1] + [vocab[-1]])

    start = time.perf_counter()
    pairs = revise.align(live, new)
    elapsed = time.perf_counter() - start

    assert elapsed < 3.0, f"align() took {elapsed:.2f}s for 600x600 — expected well under 3s"
    assert pairs.get(edited_idx, (None, 0))[0] == edited_idx
    assert pairs[edited_idx][1] >= revise._SIM_MIN


# --- regression: prefilter must not drop a row whose words are ALL too common -----------

def test_align_finds_every_row_when_shared_words_all_exceed_the_bucket_cap():
    """A 120-row field-permutation table (4 field names x 3 types x Yes/No x 5 lengths), one
    wording change per row ("10 chars" -> "10 characters"). Every row's words (field name,
    type, mandatory flag, length) are each shared by 20-60 other rows — ALL of them exceed
    the old cap-only prefilter's max_bucket, so phase 3 found ZERO candidates for ANY row
    (reproduced) and every requirement got archived + recreated instead of matched as
    'modified'. The fix must match exhaustive Jaccard exactly: all 120 rows paired correctly."""
    header = "| Field | Type | Mandatory | Length |\n| --- | --- | --- | --- |\n"
    live, new = [], []
    for field, typ, mandatory, length in itertools.product(
            ["Customer name", "Account number", "Branch code", "Phone"],
            ["Text", "Number", "Date"], ["Yes", "No"], ["10", "20", "50", "100", "200"]):
        before = f"{header}| {field} | {typ} | {mandatory} | {length} chars |"
        after = f"{header}| {field} | {typ} | {mandatory} | {length} characters |"
        live.append({"text": before, "content_hash": _hash(before), "section": "Fields", "req_id": None})
        new.append({"text": after, "content_hash": _hash(after), "section": "Fields", "req_id": None})

    pairs = revise.align(live, new)
    assert len(pairs) == 120, f"only {len(pairs)}/120 rows got a candidate at all"
    assert all(i == j for j, (i, _) in pairs.items()), "every row must pair with its OWN live row"

    items = revise.classify(live, new)
    counts = {k: sum(1 for it in items if it["change"] == k) for k in ("same", "modified", "added", "removed")}
    assert counts == {"same": 0, "modified": 120, "added": 0, "removed": 0}


# --- _row_text must detect a table-row chunk structurally, not via _looks_table's 60% rule ---

_TWO_PARAGRAPH_PREAMBLE_TABLE = """## Scope

Preamble note one: this section describes several related fields collected at onboarding.

Preamble note two: validation rules for each field are enforced client-side and server-side.

| ID | Requirement |
| --- | --- |
| FR-30 | Capture the customer's postal code during onboarding. |
| FR-31 | Validate the merchant's settlement currency nightly. |
"""


def test_row_text_detects_table_row_with_multi_paragraph_preamble():
    """With a 2-paragraph preamble, chunker._looks_table's 60%-piped-lines heuristic would
    see only 2 of 4 lines piped (50%) and miss the table shape entirely — leaving the shared
    preamble in, which makes these two UNRELATED rows look similar. _row_text must still
    isolate the data row via structure (last two lines = header, then row)."""
    rows = _table_rows(_TWO_PARAGRAPH_PREAMBLE_TABLE)
    assert len(rows) == 2
    # sanity: chunker's own heuristic misses this shape (the regression this fix targets)
    assert not chunker._looks_table(rows[0]["text"])
    row_only_0 = revise._row_text(rows[0]["text"])
    row_only_1 = revise._row_text(rows[1]["text"])
    assert row_only_0 == "| FR-30 | Capture the customer's postal code during onboarding. |"
    assert row_only_1 == "| FR-31 | Validate the merchant's settlement currency nightly. |"
    assert revise.similarity(row_only_0, row_only_1) < revise._SIM_MIN


# --- final-review regressions ---------------------------------------------------------------

def test_prose_requirement_ending_in_a_table_stays_modified_when_its_table_grows():
    """A prose requirement that merely ENDS in a small table must be compared on its full text:
    cutting it to its last table row turned "a row was added to its table" into removed + added,
    archiving the requirement and losing its id (final review, round 2)."""
    prose = ("## 3.1 Approval limits\n\n"
             "The system shall enforce approval limits for every payment request.\n\n"
             "Limits are configured per customer segment by the administrator.\n\n"
             "A request above the limit is routed to a second approver.\n\n"
             "Changes to limits are audited.\n\n")
    old = _table_rows(prose + "| Segment | Limit |\n| --- | --- |\n| Retail | 5000 |\n")
    new = _table_rows(prose + "| Segment | Limit |\n| --- | --- |\n| Retail | 5000 |\n| Corporate | 50000 |\n")
    assert len(old) == 1 and len(new) == 1, "fixture must stay a single prose chunk"
    assert revise._row_text(old[0]["text"]) == old[0]["text"]      # not cut to its last row
    kinds = [it["change"] for it in revise.classify(
        [{**old[0], "id": 1}], new)]
    assert kinds == ["modified"]


def test_match_sharing_only_common_words_is_still_found():
    """The seeded rarest words can all point at OTHER live rows while the true match shares only
    over-cap (common) words — the row must still be scored exactly, not lost as added + removed."""
    base = "the field amount is mandatory numeric value"
    live = [{"id": i, "text": f"{base} variant{i % 7} extra{i}", "content_hash": _hash(f"l{i}"),
             "req_id": None, "section": "S"} for i in range(200)]
    target = {"id": 999, "text": base, "content_hash": _hash("t"), "req_id": None, "section": "S"}
    live.append(target)
    for k in range(5):                                   # rare words that each sit in an UNRELATED row
        live[k]["text"] = f"invoices are archived monthly by finance r{k}"
    new = [{"text": base + " r0 r1 r2 r3 r4", "content_hash": _hash("n"), "req_id": None, "section": "S"}]
    pairs = revise.align(live, new)
    assert 0 in pairs and live[pairs[0][0]]["id"] == 999

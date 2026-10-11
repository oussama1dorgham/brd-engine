"""Turn parsed sections into retrieval-sized chunks.

Strategy is section-aware: each section becomes one chunk, keeping a single
requirement whole. A section longer than MAX_TOKENS is split on paragraph
boundaries with a small overlap so no requirement is silently cut in half.

Each chunk carries a SHA-256 content hash — the key that lets re-ingestion skip
unchanged chunks instead of re-embedding them.

    python -m backend.ingest.chunker data/brds/payments-brd.md
"""
from __future__ import annotations

import hashlib
import re
import sys

from backend.ingest.models import Chunk, ParsedSection
from .parser import ID_CORE, opening_req_id, parse

# Bump when chunking changes in a way that makes re-processing an existing BRD worthwhile: a
# BRD built by an older version is flagged in Manage BRDs (brd_document.chunker_version).
# 2 = structure recovery for headless / flattened BRDs (requirement units, labels, ids).
CHUNKER_VERSION = 2
MAX_TOKENS = 400          # target ceiling for a single chunk
OVERLAP_TOKENS = 60       # carried between split pieces to preserve context


def _est_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token). Real usage is returned by Voyage."""
    return max(1, round(len(text) / 4))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snippet(text: str, n: int = 48) -> str:
    """A short, single-line label derived from a chunk's own text — used as the
    citation tag when a chunk has no requirement ID (e.g. heading-less PDFs), so
    each chunk is distinguishable instead of all sharing the document title."""
    line = " ".join(text.strip().split())
    return (line[:n] + "…") if len(line) > n else line


def _is_sep(line: str) -> bool:
    """A GitHub table separator row, e.g. `| --- | :---: |` — structural, not data."""
    body = line.replace("|", "").replace(":", "").strip()
    return bool(body) and set(body) <= {"-", " "}


def _looks_table(text: str) -> bool:
    """A block whose lines are mostly `|`-delimited rows — a Markdown table. Detected
    so each row becomes its own chunk instead of being fused into one blob (which is
    what made flattened tables un-editable). One-line or single-pipe text is not one."""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return False
    piped = sum(1 for l in lines if "|" in l)
    return piped >= 2 and piped >= len(lines) * 0.6


def _split_table(text: str) -> list[str]:
    """Fan a Markdown table into one piece per DATA row, each carrying the header row
    for column context. Keeps every requirement row atomic — individually retrievable
    and editable, and never word-shredded. Content is preserved (rows are verbatim)."""
    lines = [l for l in text.splitlines() if l.strip()]
    hi = next((i for i, l in enumerate(lines) if "|" in l), None)
    if hi is None:
        return [text]
    preamble, header, rest = lines[:hi], lines[hi], lines[hi + 1:]
    data = [l for l in rest if not _is_sep(l)]
    if not data:
        return [text]                              # header only — nothing to fan out
    prefix = "\n".join([*preamble, header])
    pieces: list[str] = []
    for row in data:
        piece = f"{prefix}\n{row}"
        pieces.extend([piece] if _est_tokens(piece) <= MAX_TOKENS else _window(piece))
    return pieces


def _split(text: str) -> list[str]:
    """Keep a section whole if it fits; otherwise pack WHOLE paragraphs (never past the
    ceiling) with a short overlap. A Markdown table is fanned into one chunk per row (see
    _split_table); a single paragraph over the ceiling is cut at sentence ends (_window)."""
    if _looks_table(text):
        return _split_table(text)
    if _est_tokens(text) <= MAX_TOKENS:
        return [text]

    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    pieces: list[str] = []
    buf: list[str] = []
    def is_label(p: str) -> bool:
        return bool(is_sub_label(p) or _title_like(p) or opening_req_id(p) and _est_tokens(p) <= 30)

    for para in paras:
        if buf and _est_tokens("\n\n".join([*buf, para])) > MAX_TOKENS:
            if all(is_label(x) for x in buf):
                # only label line(s) so far ("US-01: Access by Role" before a long block): never
                # a chunk by themselves — they go into the next piece (it is windowed below)
                buf.append(para)
                continue
            # a piece never ENDS on a label line ("**Acceptance Criteria**"): the label moves
            # on with the content it introduces
            carry = [buf.pop()] if len(buf) > 1 and is_label(buf[-1]) else []
            pieces.append("\n\n".join(buf))
            # start the next piece with a tail overlap of the last paragraph (or the label)
            buf = carry or ([buf[-1]] if _est_tokens(buf[-1]) <= OVERLAP_TOKENS else [])
        buf.append(para)
    if buf:
        pieces.append("\n\n".join(buf))
    out: list[str] = []
    for piece in pieces:
        out.extend([piece] if _est_tokens(piece) <= MAX_TOKENS else _window(piece))
    return out


# A sentence ends at . ! ? (or Arabic ؟ ۔) followed by whitespace — but NOT after a bare list
# number ("3. Execution…") or a common abbreviation, which would orphan the marker.
_SENT_END = re.compile(r"(?<=[.!?؟۔])\s+")
_NOT_END = re.compile(r"(?:^|\s)(?:\(?\d{1,3}[.)]|[A-Za-z]\.|e\.g\.|i\.e\.|etc\.|vs\.|no\.|approx\.)$", re.I)


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for part in _SENT_END.split(text.strip()):
        if out and _NOT_END.search(out[-1][-16:]):       # tail only: linear on long merged runs
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part)
    return [x for x in out if x.strip()]


def _window(text: str) -> list[str]:
    """Last resort for an over-long run of text with no paragraph breaks (a flattened PDF
    page, a wall of prose): pack WHOLE sentences up to the ceiling — a chunk never ends
    mid-sentence or splits a numbered item from its number. A single sentence longer than the
    ceiling is the only thing ever cut between words."""
    max_chars = MAX_TOKENS * 4
    pieces: list[str] = []
    cur = ""
    for sent in _sentences(text):
        if len(sent) > max_chars:                       # pathological sentence: word-cut it alone
            if cur:
                pieces.append(cur)
                cur = ""
            w_cur = ""
            for w in sent.split():
                if w_cur and len(w_cur) + 1 + len(w) > max_chars:
                    pieces.append(w_cur)
                    w_cur = w
                else:
                    w_cur = f"{w_cur} {w}" if w_cur else w
            if w_cur:
                pieces.append(w_cur)
            continue
        if cur and len(cur) + 1 + len(sent) > max_chars:
            pieces.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}" if cur else sent
    if cur:
        pieces.append(cur)
    return pieces


def chunk_sections(sections: list[ParsedSection]) -> list[Chunk]:
    chunks: list[Chunk] = []
    ordinal = 0
    for s in sections:
        for piece in _split(s.text):
            chunks.append(
                Chunk(
                    ordinal=ordinal,
                    section=s.section if s.req_id else _snippet(piece),
                    req_id=s.req_id,
                    text=piece,
                    token_count=_est_tokens(piece),
                    content_hash=_hash(piece),
                )
            )
            ordinal += 1
    return chunks


# --- structure recovery for headless / flattened documents --------------------------------
# A BRD without heading styles still has structure a reader sees: a requirement starts with
# its id ("US-01: …"), a short stand-alone label line opens a block ("Shared Business Rules",
# "**Timing Preferences**"), tables are tables. `_units` rebuilds that so each requirement —
# and each labelled block — becomes its own unit, every piece of a unit keeps the unit's id,
# and the unit's label travels in section_path (which goes into the embedding).

# Labels that name a PART of a requirement, never a new block: "Acceptance Criteria" under a
# user story must stay with that story (with its id), not start an id-less section.
SUB_LABELS = frozenset(x.lower() for x in (
    "Acceptance Criteria", "Acceptance Criterion", "Description", "Details", "Notes", "Note", "Rationale",
    "Priority", "Source", "Trigger", "Inputs", "Input", "Outputs", "Output", "Preconditions",
    "Pre-conditions", "Precondition", "Postconditions", "Post-conditions", "Postcondition", "Main Flow",
    "Basic Flow", "Main Success Scenario", "Alternative Flow", "Alternative Flows", "Alternate Flow",
    "Alternate Flows", "Exception Flow", "Exception Flows", "Exceptions", "Steps", "Expected Result",
    "Expected Results", "Fit Criterion", "Validation", "Validations", "Dependencies", "Actors",
    "User Story", "Story", "Business Value", "Comments",
    "معايير القبول", "الوصف", "ملاحظات", "ملاحظة", "الشروط المسبقة", "الشروط اللاحقة", "التدفق الرئيسي",
    "التدفق البديل", "الاستثناءات", "الأولوية", "المصدر", "الخطوات", "النتيجة المتوقعة", "المدخلات", "المخرجات",
))
_SMALL_WORDS = frozenset("a an and as at by for from in into of on or per the to via vs with without".split())
_TERMINAL_PUNCT = (".", ";", ",", "!", "?", "؟", "؛", "،", "۔")
_BOLD_LINE = re.compile(r"\*\*(.+?)\*\*:?")
# an id that OPENS a requirement mid-text: after a sentence end, followed by ':' / dash / ')'
_INLINE_REQ = re.compile(rf"(?<=[.!?;:؟])\s+(?=(?:{ID_CORE})\s*[:–—)\-]\s)")


def _plain(line: str) -> str:
    m = _BOLD_LINE.fullmatch(line.strip())
    return (m.group(1) if m else line).strip()


def is_sub_label(line: str) -> bool:
    return _plain(line).rstrip(":").strip().lower() in SUB_LABELS


def _title_like(block: str) -> str | None:
    """A short stand-alone label line ("Shared Business Rules", "1. Platform Integration",
    "**Audit**", an Arabic heading line) — returned cleaned, else None. Body text, list items,
    table rows, requirement starts and sub-labels ("Acceptance Criteria") never qualify."""
    b = block.strip()
    if not b or "\n" in b or "|" in b or b.startswith(("- ", "* ", "> ", "#")):
        return None
    bold = bool(_BOLD_LINE.fullmatch(b))
    t = _plain(b)
    if not t or len(t) > 90 or is_sub_label(t):
        return None
    if t.endswith(_TERMINAL_PUNCT) or (t.endswith(":") and not bold):
        return None
    words = t.rstrip(":").split()
    if len(words) > 10:
        return None
    if bold:
        return t.rstrip(":").strip()
    latin = [w for w in words if re.match(r"[A-Za-z]", w)]
    if len(latin) * 2 > len(words):                      # mostly Latin script: Title Case label
        # (an Arabic line quoting one Latin acronym — "… مع CR …" — is judged as Arabic)
        sig = [w for w in latin if w.lower() not in _SMALL_WORDS]
        return t if sig and all(w[0].isupper() for w in sig) else None
    return t if len(words) <= 6 else None                # no letter case (Arabic): short line only


def _req_start(block: str) -> str | None:
    """The id a block OPENS with ("US-01: …", "**FR-3** The system…"), else None — never an
    id the block merely mentions ("Same as US-01, …"), and never a list whose lines each open
    a different id ("- US-01: Access …" / "- US-02: Create …" is an index of stories)."""
    lines = [ln for ln in block.strip().splitlines() if ln.strip()]
    if len(lines) > 1 and len({i for i in map(opening_req_id, lines) if i}) > 1:
        return None
    return opening_req_id(block)


# Labels that name a PART of a requirement when they appear inside one ("Business Rules" under
# US-07): a label like this stays with the open requirement. Any other label ("Reporting
# Module" + its intro) starts a block of its own.
REQ_PART_LABELS = SUB_LABELS | frozenset(x.lower() for x in (
    "Business Rules", "Business Rule", "Rules", "Validation Rules", "Constraints", "Error Handling",
    "Error Messages", "Messages", "Notifications", "Permissions", "Fields", "Data Fields", "UI Notes",
    "قواعد العمل", "القيود", "الرسائل", "الإشعارات", "الصلاحيات", "الحقول",
))


def _req_label(block: str) -> str:
    """'US-01: Access the Companion Calendar by Role' from the block that opens a requirement:
    its first line, cut before the story body ("As a …") / at the first sentence end, ≤ 100."""
    first = " ".join(_plain(block.strip().splitlines()[0]).split())
    first = re.split(r"\s(?=As an?\s)|(?<=[.!?؟])\s", first, maxsplit=1)[0]
    return first if len(first) <= 100 else first[:100].rsplit(" ", 1)[0] + "…"


def _split_inline(block: str) -> list[str]:
    """Cut a flattened run of text where a requirement id OPENS mid-paragraph
    ("… end time. US-01: Access …") — only for blocks over the ceiling (a short paragraph that
    merely mentions two ids stays whole), never inside a table."""
    if _est_tokens(block) <= MAX_TOKENS or _looks_table(block):
        return [block]
    return [p.strip() for p in _INLINE_REQ.split(block) if p.strip()]


def _units(text: str) -> list[dict]:
    """Group a section's blocks into units: {"req", "label", "blocks", "kind"} where kind is
    'req' (opens with an id), 'titled' (opens with a label line) or 'lead' (text before any).

    A label line met while a requirement is open belongs to THAT requirement when more of it
    follows — i.e. the next unit start after the label's content is another requirement
    ("US-07 …" / "Business Rules" / rules / "US-08 …"). It opens a section of its own when what
    follows is more labels or the end ("US-18 …" / "Shared Business Rules" / "Audit" / …). A
    label directly followed by a requirement is a group label: it prefixes the following
    requirements' labels and its line travels with the first of them."""
    blocks = [x for b in text.split("\n\n") if b.strip() for x in _split_inline(b.strip())]
    n = len(blocks)
    cand = [None if _looks_table(b) else _req_start(b) for b in blocks]
    # An id that opens SEVERAL blocks ("US-01: Access the calendar" in an overview, then the
    # full "US-01: …" story) is the requirement where it has the most content up to the next
    # id; the other occurrences are references — body text, never a label of their own.
    starts = [i for i, r in enumerate(cand) if r]
    size = {i: sum(len(blocks[k]) for k in range(i, nxt))
            for i, nxt in zip(starts, starts[1:] + [n])}
    best: dict[str, int] = {}
    for i in starts:
        if cand[i] not in best or size[i] > size[best[cand[i]]]:
            best[cand[i]] = i
    rids: list[str | None] = [r if r and best[r] == i else None for i, r in enumerate(cand)]
    demoted = {i for i, r in enumerate(cand) if r and rids[i] is None}
    titles = [None if (rids[i] or i in demoted or _looks_table(b)) else _title_like(b)
              for i, b in enumerate(blocks)]
    for i in range(n - 1):                   # "The roles are:" → the short lines after are items
        if blocks[i].rstrip().endswith(":") and not titles[i]:
            k = i + 1
            while k < n and titles[k]:
                titles[k] = None
                k += 1
    if titles and titles[-1]:
        titles[-1] = None                    # a trailing label with nothing under it is body text
    i = 0                                    # a RUN of 3+ label-like lines is a list under the first
    while i < n:
        j = i
        while j < n and titles[j]:
            j += 1
        if j - i >= 3:
            for k in range(i + 1, j):
                titles[k] = None
        i = max(j, i + 1)

    def next_start(i: int) -> int | None:
        """Index of the next block (after i's own content) that opens a unit."""
        k = i + 1
        while k < n and not rids[k] and not titles[k]:
            k += 1
        return k if k < n else None

    units: list[dict] = []
    pending_label: str | None = None          # a label line directly followed by another label
    group: str | None = None                  # a group label over the following requirements
    carry: list[str] = []                     # a group label's line, for the next requirement
    for i, b in enumerate(blocks):
        rid, title = rids[i], titles[i]
        cur = units[-1] if units else None
        if rid:
            label = _join_path(group, _req_label(b))
            units.append({"kind": "req", "req": rid, "label": label, "blocks": [*carry, b]})
            carry, pending_label = [], None
        elif title:
            nxt = i + 1 < n and rids[i + 1]
            if nxt:                                            # a group label: "Module B" / FR-3 …
                group, carry, pending_label = title, [b], None
                continue
            k = next_start(i)
            part = title.rstrip(":").strip().lower() in REQ_PART_LABELS or (i + 1 < n and _looks_table(blocks[i + 1]))
            if cur and cur["kind"] == "req" and part and k is not None and k > i + 1 and rids[k]:
                cur["blocks"].append(b)                        # a part of the open requirement
                continue                                       # ("Business Rules", a table caption)
            group = None
            label = f"{pending_label} / {title}" if pending_label else title
            units.append({"kind": "titled", "req": None, "label": label, "blocks": [b]})
            pending_label = title if (i + 1 < n and titles[i + 1]) else None
        elif cur:
            cur["blocks"].append(b)
        else:
            units.append({"kind": "lead", "req": None, "label": None, "blocks": [b]})
    if carry:                                  # a group label with no requirement after it
        if not units:
            units.append({"kind": "lead", "req": None, "label": None, "blocks": []})
        units[-1]["blocks"].extend(carry)
    # a parent label with nothing of its own ("Shared Business Rules" directly followed by
    # "Live Data and Active Scope") is not a chunk by itself: its line moves into the next
    # unit's text (its name is already in that unit's label)
    merged: list[dict] = []
    for u in units:
        if merged and merged[-1]["kind"] == "titled" and len(merged[-1]["blocks"]) == 1 and u["kind"] == "titled":
            u["blocks"] = merged.pop()["blocks"] + u["blocks"]
        merged.append(u)
    return merged


def _row_req(row: str, owner: str | None) -> str | None:
    """A table row's id: the id in its FIRST cell (an id column); else `owner` — the id of the
    requirement / section the table sits in (a key-value table under "UC-A05 …" is UC-A05's,
    even if a cell mentions UC-E01); else the only id the row mentions ("| Login | FR-1 |").
    A row naming several ids ("US-01 to US-03") is about them, not one of them."""
    from .parser import _REQ_ID, find_req_id
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    first = find_req_id(cells[0], head=400) if cells else None
    if first or owner:
        return first or owner
    ids = {re.sub(r"[_ ]", "-", m) for m in _REQ_ID.findall(row)}
    return ids.pop() if len(ids) == 1 else None


def _unit_pieces(blocks: list[str]) -> list[tuple[str, bool]]:
    """(piece, is_table_row) for a unit: text runs are packed by _split; each table is fanned
    into rows, with a label line right before it carried into every row as preamble."""
    out: list[tuple[str, bool]] = []
    run: list[str] = []

    def flush():
        if run:
            out.extend((p, False) for p in _split("\n\n".join(run)))
            run.clear()

    for b in blocks:
        if _looks_table(b):
            caps: list[str] = []                 # the label line(s) right before it: its caption
            while run and (_title_like(run[-1]) or is_sub_label(run[-1])):
                caps.insert(0, run.pop())
            flush()
            out.extend((p, True) for p in _split_table("\n".join([*caps, b])))
        else:
            run.append(b)
    flush()
    return out


def _unbold(text: str) -> str:
    """Drop the `**…**` marker extraction puts around a bold LABEL LINE (it only exists so the
    chunker can see the label): the stored requirement text is the BRD's own words."""
    return "\n".join(_plain(ln) if _BOLD_LINE.fullmatch(ln.strip()) else ln for ln in text.split("\n"))


def piece_req_id(text: str, fallback: str | None) -> str | None:
    """The id a stand-alone piece declares — a table row's id-column cell, else the id the text
    opens with — else `fallback`. For pieces made outside ingestion (a requirement split into
    rows by hand), so each row is cited as what it is, not as the chunk it came from."""
    last = text.strip().splitlines()[-1] if text.strip() else ""
    return opening_req_id(last if "|" in last else text) or fallback


def container_path(section_path: str | None, req_id: str | None) -> str | None:
    """`section_path` minus its last segment when that segment is the LABEL of requirement
    `req_id` itself ("Module A / FR-1: Login" → "Module A"): the place a NEW requirement added
    next to FR-1 lives — it must not be embedded as part of FR-1."""
    if not section_path or not req_id:
        return section_path
    head, _, last = section_path.rpartition(" / ")
    return (head or None) if opening_req_id(last) == req_id else section_path


def _join_path(*parts: str | None) -> str | None:
    seen: list[str] = []
    for p in parts:
        for x in (p or "").split(" / "):
            x = x.strip()
            if x and (not seen or seen[-1] != x):
                seen.append(x)
    return " / ".join(seen) or None


def build_chunks(sections: list[ParsedSection]) -> list[Chunk]:
    """Upload-path chunking (ingestion v2): one requirement per chunk, structure kept.

    Unlike `chunk_sections` (Markdown CLI path, which reuses `section` as a citation label),
    `section` stays the real top-level section (the scope use cases are grouped by), plus
    `section_path` and `page`.

    A section that fits in one chunk and holds at most one requirement is kept whole. A
    longer one — above all a HEADLESS document, which is a single section — is rebuilt into
    units (`_units`): every requirement (a block opening with its id, even mid-paragraph in
    flattened text) and every labelled block is its own unit, so two requirements never share
    a chunk and business rules never take a story's id. Every piece of a unit carries the
    unit's id and its label (in section_path, which goes into the embedding), so the 3rd
    piece of a long story is still found and cited as that story. Tables fan into one chunk
    per row; a row takes the id in its first cell, else the id of the requirement it sits in."""

    chunks: list[Chunk] = []
    ordinal = 0

    def add(text: str, req_id: str | None, path: str | None, s: ParsedSection) -> None:
        nonlocal ordinal
        text = _unbold(text)
        chunks.append(Chunk(ordinal=ordinal, section=s.section, req_id=req_id, text=text,
                            token_count=_est_tokens(text), content_hash=_hash(text),
                            section_path=path, page=s.page))
        ordinal += 1

    for s in sections:
        if _looks_table(s.text):
            for piece in _split_table(s.text):
                add(piece, _row_req(piece.splitlines()[-1], s.req_id), s.section_path, s)
            continue
        units = _units(s.text)
        if _est_tokens(s.text) <= MAX_TOKENS and (len(units) == 1 or not any(u["kind"] == "req" for u in units)):
            # fits, and is one unit (or holds no requirement of its own): keep the section
            # whole — the common case for a document with real headings. Its id is the
            # section's own (heading / opening / enclosing heading), never one it mentions.
            add(s.text, s.req_id or (units[0]["req"] if len(units) == 1 else None), s.section_path, s)
            continue
        for u in units:
            # a requirement unit has its own id; text before any unit is the section's; a
            # labelled block inherits only an id a HEADING declared ("## FR-3: Login" /
            # "Validation Rules" / …) — never a body id of the headless root section
            req = u["req"] or (s.req_id if u["kind"] == "lead" else s.heading_req_id)
            path = _join_path(s.section_path, u["label"])
            for piece, is_row in _unit_pieces(u["blocks"]):
                rid = _row_req(piece.splitlines()[-1], req) if is_row else req
                add(piece, rid, path, s)
    return chunks


def embed_input_text(title: str, section_path: str | None, section: str | None, text: str) -> str:
    """Build the embed input from raw parts (title + place in the document + text). The one
    place both the ingestion pipeline (`embed_input`, below) and requirement edits
    (`backend.requirements.edit`) build what actually gets embedded, so a requirement's vector
    means the same thing regardless of how it got there — and so the SAME (title, section_path,
    section, text) always yields the SAME embed_hash."""
    where = section_path or section or ""
    head = f"{title} › {where}" if where and where != title else title
    return f"{head}\n\n{text}"


def embed_input(doc_title: str, c: Chunk) -> str:
    """What actually gets embedded: the requirement plus its place in the document, so a
    short table row / clause carries its context into the vector. The stored `text` is the
    requirement alone (citations, edits and diffs never see this header)."""
    return embed_input_text(doc_title, c.section_path, c.section, c.text)


def embed_hash(model: str, text: str) -> str:
    return _hash(f"{model}\x00{text}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m backend.ingest.chunker <path-to-brd.md>")
    _, sections = parse(sys.argv[1])
    chunks = chunk_sections(sections)
    print(f"chunks : {len(chunks)}\n")
    print(f"{'#':>2}  {'req_id':<7}  {'tok':>4}  {'hash':<12}  preview")
    print("-" * 88)
    for c in chunks:
        preview = c.text.replace("\n", " ")[:52]
        print(f"{c.ordinal:>2}  {c.req_id or '-':<7}  {c.token_count:>4}  {c.content_hash[:12]}  {preview}")

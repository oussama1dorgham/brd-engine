"""Structure-aware extraction: uploaded .docx / .pdf bytes -> Markdown with real structure.

The Markdown feeds `parser.parse_text` (headings -> sections, section path, page) and the
section-aware chunker, so every upload yields one-requirement-per-chunk with its section,
page and requirement id — instead of the old fixed-size, structure-blind packing.

DOCX (stdlib zipfile + ElementTree; no python-docx needed):
  * headings from the document's OWN style definitions (styles.xml outline levels + names),
    so localized / custom heading styles work, not just "Heading1"
  * numbered / bulleted list items keep a "- " marker; content controls (w:sdt) are walked;
    the table of contents is skipped; text inserted with tracked changes is kept, deleted text
    is not (Word stores it as w:delText)
  * tables become GitHub Markdown (header row + one line per row) — the chunker fans each
    row into its own requirement, header attached

PDF (PyMuPDF):
  * headings from font size / weight relative to the page body text
  * tables via PyMuPDF's table finder -> Markdown; their text is not duplicated as prose
  * running headers / footers / page numbers repeated across pages are dropped
  * reading order handles right-to-left (Arabic) and two-column pages
  * `<!-- page:N -->` markers carry the page number to every chunk
  * pages with no extractable text (scanned images) are reported as warnings; OCR is not
    configured, so a fully scanned PDF is rejected with a clear message

Everything is NFKC-normalized (folds Arabic presentation forms / ligatures to plain letters
so keyword search matches what users type).
"""
from __future__ import annotations

import io
import re
import statistics
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from backend.core.config import settings

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
PAGE_MARK = "<!-- page:{n} -->"
_MAX_XML_BYTES = 80 * 1024 * 1024          # uncompressed-per-member ceiling (zip-bomb guard)
_MAX_RATIO = 120                           # max compression ratio accepted for any member
_MAX_TOTAL_BYTES = 150 * 1024 * 1024       # combined uncompressed cap across all members of one .docx
_MIN_ARABIC_WORDS = 50                     # Arabic word count that makes the RTL quality gate apply
_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿ]")


class ExtractError(ValueError):
    """The upload is not a valid / supported document (not retryable)."""


@dataclass
class Extracted:
    markdown: str
    file_type: str                  # pdf | docx
    page_count: int = 0
    warnings: list[str] = field(default_factory=list)
    headings: int = 0
    used_rtl_path: bool = False     # True once this result came out of _pdf_rtl (the pypdf/logical-order path)


def normalize(text: str) -> str:
    """NFKC + drop control chars (keep \\n / \\t) + collapse runs of spaces."""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​‎‏﻿]", "", text)
    return re.sub(r"[ \t]+", " ", text)


def _body(text: str) -> str:
    """Body text must never parse as a Markdown heading: a line starting with '#' (e.g. a table
    whose first column header is "#") gets a leading space, which the parser ignores."""
    return " " + text if text.startswith("#") else text


def _check_member_size(info: zipfile.ZipInfo) -> None:
    """Size + compression-ratio guard against a zip bomb, applied to a SINGLE member."""
    if info.file_size > _MAX_XML_BYTES or info.file_size > max(1, info.compress_size) * _MAX_RATIO:
        raise ExtractError("This .docx expands to an unsafe size and was rejected")


def _read_member(z: zipfile.ZipFile, name: str, budget: list[int]) -> bytes:
    """Bounded read of one zip member: the same size + ratio guard `sniff()` applies to
    word/document.xml, applied to EVERY member this extractor reads (styles.xml, etc.) — a
    small upload must not be able to smuggle a huge member past us and OOM the process. `budget`
    is a shared one-item [bytes remaining] cell across every member read from the same zip, so
    the COMBINED uncompressed size across all of them is capped too, not just each individually."""
    info = z.getinfo(name)                                      # KeyError if the member is absent
    _check_member_size(info)
    limit = min(_MAX_XML_BYTES, budget[0])
    with z.open(name) as f:
        data = f.read(limit + 1)                                # bounded even if the header lied
    if len(data) > limit:
        raise ExtractError("This .docx expands to an unsafe size and was rejected")
    budget[0] -= len(data)
    return data


def sniff(data: bytes, filename: str) -> str:
    """Validate by CONTENT, not just extension. Returns 'pdf' | 'docx' or raises ExtractError."""
    name = (filename or "").lower()
    if data[:5] == b"%PDF-":
        if not name.endswith(".pdf"):
            raise ExtractError("The file content is a PDF but its name does not end in .pdf")
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        if not name.endswith(".docx"):
            raise ExtractError("Only .docx Word files are supported (not .doc/.xlsx/.zip)")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                info = z.getinfo("word/document.xml")
        except (zipfile.BadZipFile, KeyError) as e:
            raise ExtractError("This .docx is damaged or is not a Word document") from e
        _check_member_size(info)
        return "docx"
    raise ExtractError("Unsupported file: upload a .pdf or .docx BRD")


# Visual-order artefacts of badly extracted Arabic: a word ending in a reversed article ("...لا"),
# or a swapped lam-alef at the start ("اال..." / "امل..." for "الا..." / "الم...").
_VISUAL_ORDER = re.compile(r"^[\u0621-\u064a]+لا$|^اال[\u0621-\u064a]|^امل[\u0621-\u064a]")


def visual_order_ratio(text: str) -> float:
    """Share of Arabic words that look like visual-order (reversed) extraction."""
    words = re.findall(r"[\u0621-\u064a]{3,}", text or "")
    return sum(1 for w in words if _VISUAL_ORDER.search(w)) / len(words) if words else 0.0


def _fails_rtl_gate(text: str) -> bool:
    """True when the Arabic in `text` looks like visual-order (reversed) extraction. Checked
    whenever the Arabic content is substantial (>= _MIN_ARABIC_WORDS words) OR the text is
    majority-Arabic — so a mostly-English BRD with Arabic sections is caught too, not only a
    fully-Arabic document (which was the only case the >30%-of-text threshold used to catch)."""
    words = re.findall(r"[ء-ي]{3,}", text or "")
    meaningful = len(words) >= _MIN_ARABIC_WORDS or _is_rtl(text)
    return meaningful and visual_order_ratio(text) > 0.05


def extract(data: bytes, filename: str) -> Extracted:
    kind = sniff(data, filename)
    out = _docx(data) if kind == "docx" else _pdf(data)
    out.markdown = normalize(out.markdown).strip()
    if not re.sub(r"<!-- page:\d+ -->|\s", "", out.markdown):
        raise ExtractError("No text could be extracted from this document"
                           + (" — it looks scanned; OCR is not configured" if kind == "pdf" else ""))
    # quality gate: never ingest unreadable (reversed) Arabic — it poisons retrieval and answers
    if _fails_rtl_gate(out.markdown):
        # A PDF that went through PyMuPDF (not already _pdf_rtl) gets one retry through the
        # logical-order (pypdf) path before rejecting. Gated on `used_rtl_path`, not on
        # _is_rtl(out.markdown) over the WHOLE document — _pdf() only samples the first 15
        # pages to pick a path, so an English cover/TOC + a substantial Arabic body stays
        # majority-English overall, routes through PyMuPDF, and would otherwise skip the
        # retry it needs. A document that already went through _pdf_rtl gets no retry (it
        # would just fail the same way again).
        if kind == "pdf" and not out.used_rtl_path:
            retry = _pdf_rtl(data, out.page_count)
            retry.markdown = normalize(retry.markdown).strip()
            if not _fails_rtl_gate(retry.markdown):
                return retry
        raise ExtractError("The Arabic text in this file could not be read in the correct order "
                           "(it would be stored reversed). Upload the Word (.docx) version of this BRD instead.")
    return out


# --- DOCX ---------------------------------------------------------------------------

def _docx_heading_styles(z: zipfile.ZipFile, budget: list[int]) -> dict[str, int]:
    """styleId -> heading level (1-based) from styles.xml (outline level or 'heading N' name).
    Missing or malformed styles.xml is not fatal — custom heading styles are a nice-to-have;
    an unsafe SIZE for it still rejects the whole upload (that's the zip-bomb guard)."""
    try:
        data = _read_member(z, "word/styles.xml", budget)
    except KeyError:
        return {}
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return {}
    levels: dict[str, int] = {}
    for st in root.iter(W + "style"):
        if st.get(W + "type") != "paragraph":
            continue
        sid = st.get(W + "styleId") or ""
        name_el = st.find(W + "name")
        name = (name_el.get(W + "val") if name_el is not None else "") or ""
        lvl_el = st.find(f"{W}pPr/{W}outlineLvl")
        if lvl_el is not None and (lvl_el.get(W + "val") or "").isdigit() and int(lvl_el.get(W + "val")) < 9:
            levels[sid] = int(lvl_el.get(W + "val")) + 1
        else:
            m = re.fullmatch(r"(?i)heading\s*(\d)", name.strip()) or re.fullmatch(r"(?i)heading(\d)", sid)
            if m:
                levels[sid] = int(m.group(1))
            elif name.strip().lower() == "title" or sid.lower() == "title":
                levels[sid] = 0
    return levels


def _ptext(p) -> str:
    """Paragraph text: w:t (incl. inside tracked insertions / hyperlinks / text boxes / the
    DESTINATION of a tracked move, w:moveTo); w:delText (tracked deletions) is naturally
    excluded, and so is the SOURCE of a tracked move (w:moveFrom) — otherwise moved text would
    appear twice (once at its old spot, once at its new one). A tab becomes a space; a line
    break (Shift+Enter, w:br / w:cr) stays a newline — authors use it to put a requirement id,
    its story and each criterion on their own lines inside ONE paragraph, and collapsing it
    fused them into one line. A page/column break is layout, so it is a space."""
    parts: list[str] = []

    def walk(el):
        if el.tag == W + "moveFrom":
            return
        if el.tag == W + "t" and el.text:
            parts.append(el.text)
        elif el.tag == W + "tab":
            parts.append(" ")
        elif el.tag == W + "br":
            parts.append(" " if el.get(W + "type") in ("page", "column") else "\n")
        elif el.tag == W + "cr":
            parts.append("\n")
        for child in el:
            walk(child)

    walk(p)
    return "".join(parts)


def _all_bold(p) -> bool:
    """Every run that carries visible text is bold (w:b, not switched off with val=0/false).
    Runs inside a tracked move's source (w:moveFrom) don't count — their text isn't shown."""
    seen = False
    moved = {id(r) for mf in p.iter(W + "moveFrom") for r in mf.iter(W + "r")}
    for r in p.iter(W + "r"):
        if id(r) in moved or not "".join(t.text or "" for t in r.iter(W + "t")).strip():
            continue
        b = r.find(f"{W}rPr/{W}b")
        if b is None or (b.get(W + "val") or "true").lower() in ("0", "false", "off"):
            return False
        seen = True
    return seen


def _pstyle(p) -> str:
    st = p.find(f"{W}pPr/{W}pStyle")
    return (st.get(W + "val") or "") if st is not None else ""


def _cell(tc) -> str:
    return " ".join(" ".join(_ptext(p) for p in tc.iter(W + "p")).split()).replace("|", "\\|")


def _tbl_rows(tbl) -> list:
    """w:tr children of a table, descending into w:sdt/w:sdtContent and w:customXml wrappers
    (template repeating-section content controls) so rows inside them aren't skipped — order
    preserved. Plain `tbl.findall(W + "tr")` only sees DIRECT children and misses these."""
    rows = []
    for el in list(tbl):
        if el.tag == W + "tr":
            rows.append(el)
        elif el.tag == W + "sdt":
            content = el.find(W + "sdtContent")
            if content is not None:
                rows.extend(_tbl_rows(content))
        elif el.tag == W + "customXml":
            rows.extend(_tbl_rows(el))
    return rows


def _row_cells(tr) -> list:
    """w:tc children of a row, descending into w:sdt/w:sdtContent and w:customXml wrappers,
    the same way `_tbl_rows` does for rows."""
    cells = []
    for el in list(tr):
        if el.tag == W + "tc":
            cells.append(el)
        elif el.tag == W + "sdt":
            content = el.find(W + "sdtContent")
            if content is not None:
                cells.extend(_row_cells(content))
        elif el.tag == W + "customXml":
            cells.extend(_row_cells(el))
    return cells


def _docx_table(tbl) -> str:
    rows: list[list[str]] = []
    for tr in _tbl_rows(tbl):
        cells = []
        for tc in _row_cells(tr):
            vm = tc.find(f"{W}tcPr/{W}vMerge")
            continued = vm is not None and (vm.get(W + "val") in (None, "continue"))
            cells.append("" if continued else _cell(tc))      # vertical merge: no duplicated text
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    if len(rows) == 1:                                       # a one-row "table" is a callout box
        return " — ".join(c for c in rows[0] if c)
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _docx_blocks(parent, levels: dict[str, int], out: list[str], stats: Counter) -> None:
    for el in list(parent):
        if el.tag == W + "sdt":                               # content control: walk its content
            content = el.find(W + "sdtContent")
            if content is not None:
                _docx_blocks(content, levels, out, stats)
        elif el.tag == W + "customXml":                       # custom XML wrapper: walk its content
            _docx_blocks(el, levels, out, stats)
        elif el.tag == W + "p":
            style = _pstyle(el)
            if style.lower().startswith("toc"):                # table of contents: skip
                continue
            lines = [ln for ln in (" ".join(x.split()) for x in _ptext(el).split("\n")) if ln]
            if not lines:
                continue
            text = " ".join(lines)
            lvl = levels.get(style)
            olvl = el.find(f"{W}pPr/{W}outlineLvl")
            if lvl is None and olvl is not None and (olvl.get(W + "val") or "").isdigit():
                lvl = int(olvl.get(W + "val")) + 1 if int(olvl.get(W + "val")) < 9 else None
            if lvl is not None and len(text) <= 200:
                out.append("#" * min(lvl + 1, 6) + " " + text)  # Title -> #, Heading 1 -> ##
                stats["headings"] += 1
            elif el.find(f"{W}pPr/{W}numPr") is not None:
                out.append("- " + text)                        # list item
            elif len(lines) > 1:
                out.extend(_body(ln) for ln in lines)          # soft line breaks: one block per line
            elif _all_bold(el) and len(text) <= 120 and not text.endswith((".", "؛", "،")):
                out.append(f"**{text}**")                      # a bold label line (headless docs'
                stats["bold_labels"] += 1                      # stand-in for a heading)
            else:
                out.append(_body(text))
        elif el.tag == W + "tbl":
            md = _docx_table(el)
            if md:
                out.append(md)


def _docx(data: bytes) -> Extracted:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            budget = [_MAX_TOTAL_BYTES]
            levels = _docx_heading_styles(z, budget)
            root = ET.fromstring(_read_member(z, "word/document.xml", budget))
        body = root.find(W + "body")
        if body is None:
            raise ExtractError("This .docx has no document body")
        out: list[str] = []
        stats: Counter = Counter()
        _docx_blocks(body, levels, out, stats)
    except ExtractError:
        raise
    except MemoryError:
        raise
    except Exception as e:  # noqa: BLE001 — any other malformed-doc failure (zlib.error,
        # LookupError from an unknown XML encoding, struct.error, etc.) must become a
        # non-retryable ExtractError, never escape as a bare exception the job runner retries
        raise ExtractError(f"This .docx is damaged or can't be read: {e}") from e
    ex = Extracted(markdown=_join_blocks(out), file_type="docx", headings=stats["headings"])
    if not stats["headings"]:
        ex.warnings.append("No headings found — structure was rebuilt from requirement IDs, label lines and tables. Check the requirements list to confirm each requirement was split correctly.")
    return ex


def _join_blocks(blocks: list[str]) -> str:
    """Blank line between blocks, but consecutive list items stay one block (a list)."""
    out: list[str] = []
    for b in blocks:
        if out and b.startswith("- ") and out[-1].split("\n")[-1].startswith("- "):
            out[-1] += "\n" + b
        else:
            out.append(b)
    return "\n\n".join(out)


# --- PDF ----------------------------------------------------------------------------

_BULLETS = re.compile(r"^[•●▪■◦○·]\s*")
_TERMINAL = (".", ":", "!", "?", "؟", "؛")


def _pdf_rtl(data: bytes, page_count: int) -> Extracted:
    """Right-to-left (Arabic) PDFs: text via pypdf (correct logical characters), structure
    rebuilt from its per-fragment font sizes — headings by size, running headers/footers and
    page numbers dropped, wrapped lines rejoined into paragraphs, page markers kept."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
    except MemoryError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ExtractError(f"This PDF could not be read ({e})") from e
    try:
        return _pdf_rtl_pages(reader, page_count)
    except ExtractError:
        raise
    except MemoryError:
        raise
    except Exception as e:  # noqa: BLE001 — any other malformed-PDF failure (pypdf's own
        # errors, struct.error, IndexError, EOFError, mupdf's FzErrorBase via a shared
        # codepath, etc.) must become a non-retryable ExtractError, not escape bare
        raise ExtractError(f"This PDF is damaged or can't be read: {e}") from e


def _pdf_rtl_pages(reader, page_count: int) -> Extracted:
    """The actual page walk/structure-rebuild for `_pdf_rtl`, split out so the caller can wrap
    it in one try/except that turns a malformed-PDF failure into a non-retryable ExtractError."""
    from .parser import find_req_id

    pages: list[list[tuple[str, float]]] = []
    for page in reader.pages:
        frags: list[tuple[str, float]] = []

        def visit(text, cm, tm, _font, size, frags=frags):
            if text:
                scale = abs(tm[3] or tm[0] or 1) * abs(cm[3] or cm[0] or 1)
                frags.append((text, round(float(size or 0) * scale, 1)))

        try:
            page.extract_text(visitor_text=visit)
        except Exception:  # noqa: BLE001 — one bad page must not sink the document
            frags = []
        lines, cur, cur_s = [], "", 0.0
        for t, s in frags:
            parts = t.split("\n")
            for k, part in enumerate(parts):
                cur += part
                if part.strip():
                    cur_s = max(cur_s, s)
                if k < len(parts) - 1:
                    if cur.strip():
                        lines.append((" ".join(cur.split()), cur_s))
                    cur, cur_s = "", 0.0
        if cur.strip():
            lines.append((" ".join(cur.split()), cur_s))
        pages.append(lines)

    ex = Extracted(markdown="", file_type="pdf", page_count=page_count)
    # running headers / footers: an edge line repeated on >= 40% of pages (>= 3 pages)
    edge = Counter(_norm_line(l) for p in pages for l, _ in (p[:2] + p[-2:]))
    repeated = {t for t, n in edge.items() if len(pages) >= 3 and n >= max(3, 0.4 * len(pages))}
    sizes = Counter()
    for p in pages:
        for l, s in p:
            if s:
                sizes[s] += len(l)
    body = sizes.most_common(1)[0][0] if sizes else 11.0
    # A font size is a HEADING LEVEL only when it is structural: bigger than body text, used on
    # >= 3 pages, and mostly for numbered lines ("1.", "1.1."). That rejects the one-off giant
    # sizes of diagram labels / callouts. Sizes found only on the cover page are the title.
    heads = {s for s in sizes if s >= body * 1.15}
    on_pages: dict[float, set[int]] = {}
    numbered: Counter = Counter()
    total: Counter = Counter()
    for n, p in enumerate(pages):
        for l, s in p:
            if s in heads and len(l) <= 160:
                on_pages.setdefault(s, set()).add(n)
                total[s] += 1
                numbered[s] += bool(re.match(r"^\d+(\.\d+)*\.?\s", l))
    title_only = {s for s, ns in on_pages.items() if ns == {0} and len(pages) > 1}
    structural = sorted((s for s, ns in on_pages.items()
                         if s not in title_only and len(ns) >= 3 and numbered[s] >= 0.5 * total[s]),
                        reverse=True)
    level_of = {**{s: 1 for s in title_only}, **{s: min(i + 2, 5) for i, s in enumerate(structural[:4])}}

    parts: list[str] = []
    scanned: list[int] = []
    for n, lines in enumerate(pages, start=1):
        lines = [(l, s) for l, s in lines
                 if _norm_line(l) not in repeated
                 and not re.fullmatch(r"[-–—\s]*\d{1,4}[-–—\s]*|page \d+( of \d+)?", l.lower())]
        if not lines:
            if reader.pages[n - 1].images:
                scanned.append(n)
            continue
        parts.append(PAGE_MARK.format(n=n))
        para: list[str] = []
        para_size = 0.0

        def flush():
            if para:
                parts.append(_body(" ".join(para)))
                para.clear()

        for l, s in lines:
            lvl = level_of.get(s)
            if lvl and len(l) <= 160:
                flush()
                parts.append("#" * lvl + " " + l)
                ex.headings += 1
                continue
            bullet = bool(_BULLETS.match(l))
            starts_new = (bullet or re.match(r"^\(?\d{1,3}[.)-]\s", l) or find_req_id(l, head=24)
                          or not para or para[-1].rstrip().endswith(_TERMINAL)
                          or (para_size and s and abs(s - para_size) > 1.0))
            if starts_new:
                flush()
                para_size = s
            para.append(("- " + _BULLETS.sub("", l)) if bullet else l)
        flush()
    ex.markdown = "\n\n".join(parts)
    if scanned:
        ex.warnings.append(f"{len(scanned)} page(s) have no extractable text (scanned images?) — "
                           f"pages {', '.join(map(str, scanned[:12]))}{'…' if len(scanned) > 12 else ''}. "
                           "OCR is not configured, so their content is not searchable.")
    if not ex.headings:
        ex.warnings.append("No headings found — structure was rebuilt from requirement IDs, label lines and tables. Check the requirements list to confirm each requirement was split correctly.")
    ex.used_rtl_path = True
    return ex


def _is_rtl(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(1 for c in letters if _ARABIC.match(c)) / len(letters) > 0.3


def _norm_line(s: str) -> str:
    return re.sub(r"\d+", "#", " ".join(s.split()).lower())


def _pdf(data: bytes) -> Extracted:
    import pymupdf as fitz  # PyMuPDF

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except MemoryError:
        raise
    except Exception as e:  # noqa: BLE001 — PyMuPDF's mupdf.FzErrorBase isn't a RuntimeError
        # subclass, so a narrower except would let it (and other malformed-file errors) escape
        raise ExtractError(f"This PDF could not be opened ({e})") from e
    if doc.needs_pass:
        raise ExtractError("This PDF is password-protected")
    if doc.page_count > settings.ingest_max_pages:
        raise ExtractError(f"This PDF has {doc.page_count} pages; the limit is {settings.ingest_max_pages}")
    # PyMuPDF returns right-to-left text in the PDF's VISUAL order: table cells come out with
    # every character reversed and prose with lam-alef / ligature pairs swapped — unreadable
    # Arabic. pypdf maps glyphs to logical characters correctly, so RTL documents take that path.
    try:
        sample = " ".join(doc[i].get_text() for i in range(min(doc.page_count, 15)))
    except MemoryError:
        raise
    except Exception as e:  # noqa: BLE001
        doc.close()
        raise ExtractError(f"This PDF is damaged or can't be read: {e}") from e
    if _is_rtl(sample):
        n = doc.page_count
        doc.close()
        return _pdf_rtl(data, n)

    try:
        return _pdf_pymupdf(doc, fitz)
    except ExtractError:
        raise
    except MemoryError:
        raise
    except Exception as e:  # noqa: BLE001 — any other malformed-PDF failure must become a
        # non-retryable ExtractError, never escape as a bare exception the job runner retries
        raise ExtractError(f"This PDF is damaged or can't be read: {e}") from e


def _pdf_pymupdf(doc, fitz) -> Extracted:
    """The actual page walk/structure-rebuild for the (non-RTL) PyMuPDF path, split out so the
    caller can wrap it in one try/except that turns a malformed-PDF failure into a
    non-retryable ExtractError instead of a bare PyMuPDF exception the job runner would retry."""
    from .chunker import is_sub_label

    pages: list[dict] = []
    sizes: Counter = Counter()
    with doc:
        for pno, page in enumerate(doc, start=1):
            height, width = page.rect.height, page.rect.width
            tables, tboxes = [], []
            try:
                for t in page.find_tables().tables:
                    rows = [[" ".join((c or "").split()).replace("|", "\\|") for c in r] for r in t.extract()]
                    rows = [r for r in rows if any(r)]
                    if len(rows) >= 2:
                        w = max(len(r) for r in rows)
                        rows = [r + [""] * (w - len(r)) for r in rows]
                        md = "\n".join(["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * w) + " |"]
                                       + ["| " + " | ".join(r) + " |" for r in rows[1:]])
                        tables.append((t.bbox[1], t.bbox[0], t.bbox[2], md))
                        tboxes.append(fitz.Rect(t.bbox))
            except Exception:  # noqa: BLE001 — table detection is best-effort
                pass
            blocks = []
            for b in page.get_text("dict")["blocks"]:
                if b.get("type") != 0:
                    continue
                r = fitz.Rect(b["bbox"])
                if any(r.intersects(tb) for tb in tboxes):
                    continue                                   # its text is in the table markdown
                spans = [s for ln in b["lines"] for s in ln["spans"] if s["text"].strip()]
                if not spans:
                    continue
                text = " ".join(" ".join(s["text"] for s in ln["spans"]).strip() for ln in b["lines"]).strip()
                size = max(s["size"] for s in spans)
                bold = all(s["flags"] & 16 for s in spans)
                for s in spans:
                    sizes[round(s["size"], 1)] += len(s["text"])
                blocks.append({"y0": r.y0, "x0": r.x0, "x1": r.x1, "text": text, "size": size, "bold": bold,
                               "edge": r.y1 < height * 0.08 or r.y0 > height * 0.92})
            pages.append({"n": pno, "w": width, "blocks": blocks, "tables": tables,
                          "images": bool(page.get_images()), "rtl": _is_rtl(" ".join(b["text"] for b in blocks))})

    ex = Extracted(markdown="", file_type="pdf", page_count=len(pages))
    body_size = statistics.median([s for s, n in sizes.items() for _ in range(min(n, 50))]) if sizes else 10.0
    heading_sizes = sorted({round(b["size"], 1) for p in pages for b in p["blocks"]
                            if b["size"] >= body_size * 1.15 and len(b["text"]) <= 160}, reverse=True)
    level_of = {s: min(i + 2, 5) for i, s in enumerate(heading_sizes[:4])}   # biggest -> ##

    # running headers/footers: an edge line repeated on >= 40% of pages (>= 3 pages)
    edge_counts = Counter(_norm_line(b["text"]) for p in pages for b in p["blocks"] if b["edge"])
    repeated = {t for t, n in edge_counts.items() if len(pages) >= 3 and n >= max(3, 0.4 * len(pages))}

    parts: list[str] = []
    scanned: list[int] = []
    for p in pages:
        blocks = [b for b in p["blocks"] if not (b["edge"] and _norm_line(b["text"]) in repeated)]
        blocks = [b for b in blocks if not re.fullmatch(r"[-–—\s]*\d{1,4}[-–—\s]*|page \d+( of \d+)?", b["text"].lower())]
        if not blocks and not p["tables"]:
            if p["images"]:
                scanned.append(p["n"])
            continue
        mid = p["w"] / 2
        left = [b for b in blocks if b["x1"] <= mid + 5]
        right = [b for b in blocks if b["x0"] >= mid - 5]
        two_col = len(left) >= 4 and len(right) >= 4 and len(left) + len(right) >= 0.8 * len(blocks)
        items = [(b["y0"], b["x0"], b["x1"], b) for b in blocks] + [(t[0], t[1], t[2], t[3]) for t in p["tables"]]

        def col(it):
            return 0 if it[2] <= mid + 5 else 1

        if two_col:
            order = (lambda it: (1 - col(it), it[0])) if p["rtl"] else (lambda it: (col(it), it[0]))
        else:
            order = (lambda it: (round(it[0] / 3), -it[2])) if p["rtl"] else (lambda it: (round(it[0] / 3), it[1]))
        items.sort(key=order)

        parts.append(PAGE_MARK.format(n=p["n"]))
        for it in items:
            obj = it[3]
            if isinstance(obj, str):
                parts.append(obj)
                continue
            lvl = level_of.get(round(obj["size"], 1))
            is_head = (lvl is not None or (obj["bold"] and len(obj["text"]) <= 90
                                           and not obj["text"].rstrip().endswith((".", ":", "،", "؛"))))
            if is_head and lvl is None and is_sub_label(obj["text"]):
                # a BOLD line "Acceptance Criteria" under a story is part of that story — a
                # heading here would cut the criteria off into an id-less section of their own.
                # A heading the font SIZE marks (a real "Dependencies" chapter) stays a heading.
                parts.append(f"**{obj['text'].strip()}**")
            elif is_head and len(obj["text"]) <= 160:
                parts.append("#" * (lvl or 4) + " " + obj["text"])
                ex.headings += 1
            else:
                parts.append(_body(obj["text"]))
    ex.markdown = "\n\n".join(parts)
    if scanned:
        ex.warnings.append(f"{len(scanned)} page(s) have no extractable text (scanned images?) — "
                           f"pages {', '.join(map(str, scanned[:12]))}{'…' if len(scanned) > 12 else ''}. "
                           "OCR is not configured, so their content is not searchable.")
    if not ex.headings:
        ex.warnings.append("No headings found — structure was rebuilt from requirement IDs, label lines and tables. Check the requirements list to confirm each requirement was split correctly.")
    return ex

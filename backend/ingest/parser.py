"""Structure-aware parser for Markdown BRDs.

Turns a `.md` file into a DocumentMeta (from YAML-style front matter) plus a list
of ParsedSection blocks, one per heading that has body text. Requirement IDs like
FR-14 / NFR-3 are detected from the heading (or the body) so every block stays
traceable back to a specific requirement.

Run it directly to see exactly what parsing produces:

    python -m backend.ingest.parser data/brds/payments-brd.md
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from backend.ingest.models import DocumentMeta, ParsedSection

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_PAGE = re.compile(r"^<!-- page:(\d+) -->$")
# Requirement ids: FR-14, NFR 3, BR_2a, REQ-12, FR-1.2, US-7, SYS-4, DR-1, UC-A1-B …
# Upper-case prefixes only (avoids matching ordinary words); normalized to "FR-14" form.
_REQ_ID = re.compile(
    r"\b((?:FR|NFR|BR|UR|SR|REQ|US|SYS|DR|FN)[-_ ]?\d+(?:\.\d+)*[a-z]?|UC-[A-Z0-9]+(?:-[A-Z0-9]+)*)\b")


def find_req_id(text: str | None, head: int = 160) -> str | None:
    """The first requirement id in the head of `text`, normalized (e.g. 'FR 3' -> 'FR-3')."""
    m = _REQ_ID.search((text or "")[:head])
    return re.sub(r"[_ ]", "-", m.group(1)) if m else None


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Pull a leading `--- ... ---` block of key: value lines off the top."""
    meta: dict[str, str] = {}
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                for kv in lines[1:i]:
                    if ":" in kv:
                        key, _, value = kv.partition(":")
                        meta[key.strip().lower()] = value.strip()
                return meta, "\n".join(lines[i + 1 :])
    return meta, text


def parse(path: str | Path) -> tuple[DocumentMeta, list[ParsedSection]]:
    raw = Path(path).read_text(encoding="utf-8")
    fm, body = _split_front_matter(raw)

    meta = DocumentMeta(
        title=fm.get("title", Path(path).stem),
        project=fm.get("project", "unknown"),
        version=fm.get("version", "v1"),
        status=fm.get("status", "draft"),
    )
    return meta, parse_text(body, meta.title)


def parse_text(body: str, title: str) -> list[ParsedSection]:
    """Markdown body -> sections. Tracks the full heading path (section_path), the top-level
    section (level <= 2 heading: the scope use cases are grouped by) and the PDF page from
    `<!-- page:N -->` markers emitted by `extract`."""
    blocks: list[dict] = []
    section_ctx = title
    stack: list[tuple[int, str]] = []            # (level, heading) of the open headings
    page: int | None = None
    # Start with a default block so content BEFORE the first heading — or a document
    # with no headings at all — is still captured (otherwise it would be dropped).
    current: dict = {"title": title, "section": title, "path": None, "page": None, "lines": []}
    for line in body.splitlines():
        pm = _PAGE.match(line.strip())
        if pm:
            page = int(pm.group(1))
            if current["page"] is None and not "".join(current["lines"]).strip():
                current["page"] = page
            continue
        m = _HEADING.match(line)
        if m:
            blocks.append(current)
            level = len(m.group(1))
            head = m.group(2).strip()
            if level <= 2:                       # a top-level section sets the context
                section_ctx = head
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, head))
            path = " / ".join(h for lv, h in stack if lv > 1 or len(stack) == 1)
            current = {"title": head, "section": section_ctx, "path": path, "page": page, "lines": []}
        else:
            if current["page"] is None and line.strip():
                current["page"] = page
            current["lines"].append(line)
    blocks.append(current)

    sections: list[ParsedSection] = []
    ordinal = 0
    for b in blocks:
        text = "\n".join(b["lines"]).strip()
        if not text:                             # skip headings that only hold sub-headings
            continue
        sections.append(
            ParsedSection(
                ordinal=ordinal,
                title=b["title"],
                section=b["section"],
                req_id=find_req_id(b["title"]) or find_req_id(text),
                text=text,
                section_path=b["path"],
                page=b["page"],
            )
        )
        ordinal += 1
    return sections


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m backend.ingest.parser <path-to-brd.md>")
    meta, sections = parse(sys.argv[1])
    print(f"document : {meta.title!r}  project={meta.project}  version={meta.version}  status={meta.status}")
    print(f"sections : {len(sections)}\n")
    print(f"{'#':>2}  {'req_id':<7}  {'section':<26}  {'chars':>5}  title")
    print("-" * 88)
    for s in sections:
        print(f"{s.ordinal:>2}  {s.req_id or '-':<7}  {s.section[:26]:<26}  {len(s.text):>5}  {s.title}")

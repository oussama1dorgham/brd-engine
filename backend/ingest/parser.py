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

from ..models import DocumentMeta, ParsedSection

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_REQ_ID = re.compile(r"\b((?:FR|NFR|BR|UR)-\d+|UC-[A-Z0-9]+(?:-[A-Z0-9]+)*)\b")


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

    # Walk the headings, buffering each heading's body until the next heading.
    blocks: list[dict] = []
    section_ctx = meta.title
    # Start with a default block so content BEFORE the first heading — or a document
    # with no headings at all — is still captured (otherwise it would be dropped).
    current: dict = {"title": meta.title, "section": meta.title, "lines": []}
    for line in body.splitlines():
        m = _HEADING.match(line)
        if m:
            blocks.append(current)
            level = len(m.group(1))
            title = m.group(2).strip()
            if level <= 2:                       # a top-level section sets the context
                section_ctx = title
            current = {"title": title, "section": section_ctx, "lines": []}
        else:
            current["lines"].append(line)
    blocks.append(current)

    sections: list[ParsedSection] = []
    ordinal = 0
    for b in blocks:
        text = "\n".join(b["lines"]).strip()
        if not text:                             # skip headings that only hold sub-headings
            continue
        hit = _REQ_ID.search(b["title"]) or _REQ_ID.search(text)
        sections.append(
            ParsedSection(
                ordinal=ordinal,
                title=b["title"],
                section=b["section"],
                req_id=hit.group(1) if hit else None,
                text=text,
            )
        )
        ordinal += 1
    return meta, sections


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

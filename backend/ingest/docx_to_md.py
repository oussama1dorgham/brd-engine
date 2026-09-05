"""Convert a Word .docx BRD into the Markdown format our pipeline ingests.

Uses only the standard library (zipfile + ElementTree) so it works anywhere,
including Python 3.14. Word heading styles become Markdown headings, so the
existing structure-aware parser can split the document into sections. Tables are
flattened to " | "-joined lines. Front matter is added so the doc gets identity.

    python -m backend.ingest.docx_to_md --downloads-glob "*BRD_1.3*.docx" \\
        --project directives --version 1.3 --title "Directives Follow-up Management (BRD 1.3)"
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _text(el) -> str:
    return "".join(t.text or "" for t in el.iter(W + "t"))


def _style(p) -> str:
    ppr = p.find(W + "pPr")
    if ppr is not None:
        st = ppr.find(W + "pStyle")
        if st is not None:
            return (st.get(W + "val") or "").lower()
    return ""


def docx_to_markdown(path: str | Path) -> str:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(W + "body")
    lines: list[str] = []
    for el in list(body):
        if el.tag == W + "p":
            text = _text(el).strip()
            if not text:
                continue
            style = _style(el)
            m = re.search(r"heading(\d)", style)
            if style == "title":
                lines.append("# " + text)
            elif m:
                level = min(int(m.group(1)) + 1, 6)   # Word Heading1 -> Markdown ##
                lines.append("#" * level + " " + text)
            else:
                lines.append(text)
        elif el.tag == W + "tbl":
            for row in el.findall(W + "tr"):
                cells = [_text(c).strip() for c in row.findall(W + "tc")]
                cells = [c for c in cells if c]
                if cells:
                    lines.append(" | ".join(cells))
    return "\n\n".join(lines)


def import_docx(src: Path, project: str, version: str, title: str, out_dir: str = "data/brds") -> Path:
    body = docx_to_markdown(src)
    front = f"---\ntitle: {title}\nproject: {project}\nversion: {version}\nstatus: approved\n---\n\n"
    out = Path(out_dir) / f"{project}-brd.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(front + body, encoding="utf-8")
    return out


def _arg(argv: list[str], name: str, default: str | None = None) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else default


if __name__ == "__main__":
    argv = sys.argv[1:]
    src_path: Path | None = None

    pattern = _arg(argv, "--downloads-glob")
    direct = _arg(argv, "--in")
    if pattern:
        matches = sorted((Path.home() / "Downloads").glob(pattern))
        if not matches:
            raise SystemExit(f"no file matching {pattern!r} in Downloads")
        src_path = matches[0]
    elif direct:
        src_path = Path(direct)
    else:
        raise SystemExit("provide --downloads-glob PATTERN or --in PATH")

    project = _arg(argv, "--project", "imported")
    version = _arg(argv, "--version", "v1")
    title = _arg(argv, "--title", src_path.stem)

    body = docx_to_markdown(src_path)
    out = import_docx(src_path, project, version, title)
    approx_tokens = round(len(body) / 4)
    print(f"source : {src_path.name}")
    print(f"output : {out}")
    print(f"chars  : {len(body)}   (~{approx_tokens} tokens)")
    print(f"heads  : {body.count(chr(10) + '#') + (1 if body.startswith('#') else 0)} markdown headings detected")

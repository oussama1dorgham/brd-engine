"""Convert a PDF BRD into the Markdown format our pipeline ingests (stdlib + pypdf).

PDFs rarely carry heading structure, so we extract text page by page, rejoin
wrapped lines into paragraphs (blank line = paragraph break), and let the
section-aware chunker split by size. Adds the same front matter as docx_to_md,
so the document gets its project/version identity.

    python -m backend.ingest.pdf_to_md --in "C:\\path\\file.pdf" --project aoun-sub --version v1 --title "Aoun Sub BRD"
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from pypdf import PdfReader


def pdf_to_markdown(path: str | Path) -> str:
    reader = PdfReader(str(path))
    paras: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        for block in re.split(r"\n\s*\n", text):        # blank line = paragraph break
            block = re.sub(r"[ \t]*\n[ \t]*", " ", block).strip()   # rejoin wrapped lines
            if block:
                paras.append(block)
    return "\n\n".join(paras)


def import_pdf(src: str | Path, project: str, version: str, title: str,
               out_dir: str = "data/brds") -> Path:
    front = f"---\ntitle: {title}\nproject: {project}\nversion: {version}\nstatus: approved\n---\n\n"
    out = Path(out_dir) / f"{project}-brd.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(front + pdf_to_markdown(src), encoding="utf-8")
    return out


def _arg(argv: list[str], name: str, default: str | None = None) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else default


if __name__ == "__main__":
    argv = sys.argv[1:]
    src = _arg(argv, "--in")
    if not src:
        raise SystemExit("usage: --in <path.pdf> --project <name> [--version v1] [--title ...]")
    project = _arg(argv, "--project", "imported")
    version = _arg(argv, "--version", "v1")
    title = _arg(argv, "--title", Path(src).stem)

    body = pdf_to_markdown(src)
    out = import_pdf(src, project, version, title)
    print(f"source : {Path(src).name}")
    print(f"output : {out}")
    print(f"chars  : {len(body)}   (~{round(len(body) / 4)} tokens)")
    print(f"paras  : {body.count(chr(10) + chr(10)) + 1 if body else 0}")

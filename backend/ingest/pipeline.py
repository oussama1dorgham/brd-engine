"""End-to-end ingestion: parse -> chunk -> embed -> store.

    python -m backend.ingest.pipeline data/brds/payments-brd.md   # one file
    python -m backend.ingest.pipeline data/brds                    # a folder of .md
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from .chunker import chunk_sections
from .parser import parse
from .store import upsert


def ingest_file(path: Path) -> None:
    meta, sections = parse(path)
    chunks = chunk_sections(sections)
    doc_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    doc_id, s = upsert(meta, chunks, doc_sha)
    print(f"[{path.name}] {meta.project}/{meta.title} {meta.version}  ->  doc_id={doc_id}")
    print(
        f"    chunks={s['total']}  embedded={s['embedded']}  skipped={s['skipped']}  "
        f"deleted={s['deleted']}  voyage_tokens={s['tokens']}"
    )


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        raise SystemExit("usage: python -m backend.ingest.pipeline <file.md | dir>")
    target = Path(argv[1])
    files = sorted(target.glob("*.md")) if target.is_dir() else [target]
    if not files:
        raise SystemExit(f"no .md files found at {target}")
    for f in files:
        ingest_file(f)


if __name__ == "__main__":
    main(sys.argv)

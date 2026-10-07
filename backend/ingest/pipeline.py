"""CLI ingestion entry point.

Primary path — real documents (.pdf / .docx) — routes through the real, durable job
system (backend.ingest.jobs.submit), exactly like the UI's Manage BRDs upload: the file
is validated, stored in object storage, and queued as an `ingest_job` that the background
worker processes (extract -> chunk -> embed -> store | compare). This never hard-deletes,
never bypasses ingestion v2, and works against brd_real.

    python -m backend.ingest.pipeline <file.pdf|file.docx> --owner-id 6
    python -m backend.ingest.pipeline <file.pdf|file.docx> --owner-id 6 --title "My BRD"
    python -m backend.ingest.pipeline <file.pdf|file.docx> --owner-id 6 --project <slug>   # new version of an existing BRD
    python -m backend.ingest.pipeline <file.pdf|file.docx> --owner-id 6 --as-new           # force a separate BRD on a title clash
    python -m backend.ingest.pipeline <file.pdf|file.docx> --owner-id 6 --wait             # poll until done/review/failed

A `--project` upload is staged as a revision for review in the UI (Manage BRDs), not
applied automatically — `--wait` reports once it reaches "review", it does not apply it.

Secondary path — Markdown (.md) — kept ONLY for the synthetic demo/golden-set DB (`brd`;
see CLAUDE.md "Databases" and docs/INGESTION.md). That DB predates ingestion v2 (no
owner_id, no object storage, no ingest_job, no brd_chunk_archive) and is reseeded directly
through backend.ingest.store.upsert. It never hard-deletes: re-ingesting a file with fewer
requirements than are already stored is refused (backend.ingest.store.ShrinkRejected)
rather than deleting the extras. Real BRDs must use the --owner-id path above or the
Manage BRDs upload in the UI, never this Markdown path.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

from .chunker import chunk_sections
from .parser import parse
from .store import ShrinkRejected, WrongDatabase, upsert

_POLL_SECONDS = 2.0
_WAIT_TIMEOUT_SECONDS = 1800.0
_STILL_QUEUED_HINT_SECONDS = 60.0
_TERMINAL = {"done", "review", "failed", "canceled"}


def _ingest_markdown(path: Path) -> None:
    meta, sections = parse(path)
    chunks = chunk_sections(sections)
    doc_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        doc_id, s = upsert(meta, chunks, doc_sha)
    except ShrinkRejected as e:
        raise SystemExit(f"[{path.name}] refused: {e}")
    except WrongDatabase as e:
        raise SystemExit(str(e))
    print(f"[{path.name}] {meta.project}/{meta.title} {meta.version}  ->  doc_id={doc_id}")
    print(f"    chunks={s['total']}  embedded={s['embedded']}  skipped={s['skipped']}  voyage_tokens={s['tokens']}")


def _submit_and_wait(*, owner_id: int, by: int, filename: str, data: bytes, title: str | None,
                      project: str | None, as_new: bool, wait: bool) -> dict:
    from . import jobs

    try:
        result = jobs.submit(owner_id, by, data, filename, title=title, target_project=project, as_new=as_new)
    except jobs.UploadRejected as e:
        raise SystemExit(f"[{filename}] rejected ({e.status}): {e}")

    if result.get("unchanged"):
        print(f"[{filename}] unchanged — {result['message']}")
        return result

    print(f"[{filename}] job #{result['job_id']} queued  project={result['project']}  "
          f"title={result['title']}  mode={result['mode']}")
    if not wait:
        print("    (not waiting — pass --wait to poll until the worker finishes)")
        return result

    start = time.time()
    deadline = start + _WAIT_TIMEOUT_SECONDS
    last_stage = None
    still_queued_hinted = False
    job = None
    while time.time() < deadline:
        rows = jobs.jobs(owner_id, project=result["project"], limit=10)
        job = next((j for j in rows if j["id"] == result["job_id"]), None)
        if job is None:
            raise SystemExit(f"[{filename}] job #{result['job_id']} vanished")
        if job["stage"] != last_stage:
            print(f"    stage={job['stage']}  {job['done']}/{job['total'] or '?'}")
            last_stage = job["stage"]
        if job["status"] in _TERMINAL:
            break
        if (not still_queued_hinted and job["status"] == "queued"
                and time.time() - start >= _STILL_QUEUED_HINT_SECONDS):
            print("    Still queued — is the API server running? The ingestion worker runs inside it.")
            still_queued_hinted = True
        time.sleep(_POLL_SECONDS)
    else:
        status = job["status"] if job else "?"
        raise SystemExit(f"[{filename}] job #{result['job_id']} timed out after "
                          f"{_WAIT_TIMEOUT_SECONDS:.0f}s — still {status}")

    print(f"[{filename}] job #{job['id']} -> {job['status']}")
    if job.get("error"):
        print(f"    error: {job['error']}")
    if job.get("warnings"):
        print(f"    warnings: {job['warnings']}")
    if job["status"] == "failed":
        raise SystemExit(1)
    return job


def _parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m backend.ingest.pipeline")
    ap.add_argument("file", help="path to a .pdf/.docx (real BRD) or .md (demo/golden-set DB only)")
    ap.add_argument("--owner-id", type=int, default=None, help="required for .pdf/.docx — the uploading user's id")
    ap.add_argument("--by", type=int, default=None, help="acting user id for .pdf/.docx — defaults to --owner-id")
    ap.add_argument("--title", default=None, help="BRD title for .pdf/.docx (derived from the filename if omitted)")
    ap.add_argument("--project", default=None,
                     help="existing BRD's project slug for .pdf/.docx — upload is staged as a NEW VERSION of it")
    ap.add_argument("--as-new", action="store_true", help=".pdf/.docx: force a separate BRD even if the title clashes")
    ap.add_argument("--wait", action="store_true", help=".pdf/.docx: poll the job until done/review/failed")
    return ap.parse_args(argv[1:])


def main(argv: list[str]) -> None:
    args = _parse(argv)
    path = Path(args.file)
    if not path.is_file():
        raise SystemExit(f"not a file: {path}")

    suffix = path.suffix.lower()
    if suffix == ".md":
        if args.owner_id or args.project or args.as_new or args.wait or args.by:
            raise SystemExit(
                ".md ingestion is the legacy demo/golden-set path (backend.ingest.store) and does not take "
                "--owner-id/--by/--project/--as-new/--wait — those apply to .pdf/.docx, which route through "
                "the real upload flow (backend.ingest.jobs). Upload a .pdf/.docx for a real BRD instead."
            )
        _ingest_markdown(path)
        return

    if suffix not in (".pdf", ".docx"):
        raise SystemExit(
            f"unsupported file type {suffix!r} — use .pdf or .docx for a real BRD (goes through the upload "
            f"flow) or .md for the demo/golden-set DB only"
        )
    if not args.owner_id:
        raise SystemExit("--owner-id is required for .pdf/.docx ingestion (it routes through the real "
                          "upload/job system, same as Manage BRDs)")

    _submit_and_wait(owner_id=args.owner_id, by=args.by or args.owner_id, filename=path.name,
                      data=path.read_bytes(), title=args.title, project=args.project,
                      as_new=args.as_new, wait=args.wait)


if __name__ == "__main__":
    main(sys.argv)

"""CLI argument parsing for backend.ingest.pipeline (pure logic, no DB/network).

.pdf/.docx must route through the real job system (backend.ingest.jobs.submit) — jobs.submit
and jobs.jobs are monkeypatched so no DB/object-storage call ever happens. .md stays on the
legacy demo/golden-set path (backend.ingest.store.upsert) and refuses the upload-flow flags.
"""
from __future__ import annotations

import pytest

from backend.ingest import jobs as real_jobs
from backend.ingest import pipeline


def _write(tmp_path, name: str, content: bytes = b"stub") -> str:
    p = tmp_path / name
    p.write_bytes(content)
    return str(p)


# --- file-type / flag validation --------------------------------------------------------

def test_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        pipeline.main(["prog", str(tmp_path / "nope.pdf")])


def test_unsupported_extension_rejected(tmp_path):
    path = _write(tmp_path, "brd.txt")
    with pytest.raises(SystemExit, match="unsupported file type"):
        pipeline.main(["prog", path])


def test_pdf_without_owner_id_rejected(tmp_path):
    path = _write(tmp_path, "brd.pdf")
    with pytest.raises(SystemExit, match="--owner-id is required"):
        pipeline.main(["prog", path])


def test_docx_without_owner_id_rejected(tmp_path):
    path = _write(tmp_path, "brd.docx")
    with pytest.raises(SystemExit, match="--owner-id is required"):
        pipeline.main(["prog", path])


@pytest.mark.parametrize("flag,extra", [
    ("--owner-id", ["6"]), ("--project", ["p"]), ("--as-new", []), ("--wait", []),
])
def test_markdown_rejects_upload_flow_flags(tmp_path, flag, extra):
    path = _write(tmp_path, "brd.md", b"# Title\n")
    with pytest.raises(SystemExit, match="does not take"):
        pipeline.main(["prog", path, flag, *extra])


# --- .pdf/.docx routes through jobs.submit (monkeypatched) --------------------------------

def test_pdf_calls_jobs_submit(tmp_path, monkeypatch):
    path = _write(tmp_path, "brd.pdf", b"%PDF-1.7 stub")
    calls = {}

    def fake_submit(owner_id, by, data, filename, *, title=None, target_project=None, as_new=False):
        calls["args"] = (owner_id, by, data, filename, title, target_project, as_new)
        return {"ok": True, "job_id": 1, "project": "my-brd", "title": "My Brd",
                "mode": "new", "status": "queued"}

    monkeypatch.setattr(real_jobs, "submit", fake_submit)

    pipeline.main(["prog", path, "--owner-id", "6", "--title", "My Brd"])

    owner_id, by, data, filename, title, target_project, as_new = calls["args"]
    assert owner_id == 6
    assert by == 6                      # defaults --by to --owner-id
    assert data == b"%PDF-1.7 stub"
    assert filename == "brd.pdf"
    assert title == "My Brd"
    assert target_project is None
    assert as_new is False


def test_pdf_passes_project_as_new_and_explicit_by(tmp_path, monkeypatch):
    path = _write(tmp_path, "brd.docx", b"stub")
    calls = {}

    def fake_submit(owner_id, by, data, filename, *, title=None, target_project=None, as_new=False):
        calls["args"] = (owner_id, by, target_project, as_new)
        return {"ok": True, "job_id": 2, "project": "existing-brd", "title": "Existing",
                "mode": "revision", "status": "queued"}

    monkeypatch.setattr(real_jobs, "submit", fake_submit)

    pipeline.main(["prog", path, "--owner-id", "6", "--by", "9",
                   "--project", "existing-brd", "--as-new"])

    owner_id, by, target_project, as_new = calls["args"]
    assert owner_id == 6
    assert by == 9
    assert target_project == "existing-brd"
    assert as_new is True


def test_unchanged_upload_does_not_poll(tmp_path, monkeypatch):
    path = _write(tmp_path, "brd.pdf", b"stub")
    monkeypatch.setattr(real_jobs, "submit", lambda *a, **k: {
        "unchanged": True, "project": "p", "title": "t", "message": "identical"})

    def fail_jobs(*a, **k):
        raise AssertionError("jobs.jobs() should not be called when the upload is unchanged")

    monkeypatch.setattr(real_jobs, "jobs", fail_jobs)
    pipeline.main(["prog", path, "--owner-id", "6", "--wait"])  # must not raise/hang


def test_wait_polls_until_done(tmp_path, monkeypatch):
    path = _write(tmp_path, "brd.pdf", b"stub")
    monkeypatch.setattr(real_jobs, "submit", lambda *a, **k: {
        "ok": True, "job_id": 5, "project": "p", "title": "t", "mode": "new", "status": "queued"})

    responses = iter([
        [{"id": 5, "project": "p", "status": "running", "stage": "extract", "done": 0, "total": None,
          "error": None, "warnings": []}],
        [{"id": 5, "project": "p", "status": "done", "stage": "store", "done": 3, "total": 3,
          "error": None, "warnings": []}],
    ])
    monkeypatch.setattr(real_jobs, "jobs", lambda *a, **k: next(responses))
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_a: None)

    pipeline.main(["prog", path, "--owner-id", "6", "--wait"])  # must not raise


def test_wait_exits_nonzero_on_failed_job(tmp_path, monkeypatch):
    path = _write(tmp_path, "brd.pdf", b"stub")
    monkeypatch.setattr(real_jobs, "submit", lambda *a, **k: {
        "ok": True, "job_id": 7, "project": "p", "title": "t", "mode": "new", "status": "queued"})
    monkeypatch.setattr(real_jobs, "jobs", lambda *a, **k: [
        {"id": 7, "project": "p", "status": "failed", "stage": "embed", "done": 1, "total": 3,
         "error": "voyage down", "warnings": []}])
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_a: None)

    with pytest.raises(SystemExit):
        pipeline.main(["prog", path, "--owner-id", "6", "--wait"])


def test_submit_rejection_exits_cleanly(tmp_path, monkeypatch):
    path = _write(tmp_path, "brd.pdf", b"stub")

    def fake_submit(*a, **k):
        raise real_jobs.UploadRejected(409, "This BRD is already being processed")

    monkeypatch.setattr(real_jobs, "submit", fake_submit)
    with pytest.raises(SystemExit, match="rejected"):
        pipeline.main(["prog", path, "--owner-id", "6"])


# --- .md stays on the legacy demo/golden-set path (backend.ingest.store.upsert) -----------

def test_markdown_calls_store_upsert(tmp_path, monkeypatch):
    path = _write(tmp_path, "brd.md", b"# Title\n")
    fake_meta = type("Meta", (), {"project": "p", "title": "t", "version": "v1"})()
    monkeypatch.setattr(pipeline, "parse", lambda _p: (fake_meta, ["sections"]))
    monkeypatch.setattr(pipeline, "chunk_sections", lambda _s: ["chunk"])

    calls = {}

    def fake_upsert(meta, chunks, doc_sha):
        calls["args"] = (meta, chunks, doc_sha)
        return 42, {"total": 1, "embedded": 1, "skipped": 0, "tokens": 10}

    monkeypatch.setattr(pipeline, "upsert", fake_upsert)
    pipeline.main(["prog", path])

    meta, chunks, _sha = calls["args"]
    assert meta is fake_meta
    assert chunks == ["chunk"]


def test_markdown_shrink_rejected_exits_cleanly(tmp_path, monkeypatch):
    path = _write(tmp_path, "brd.md", b"# Title\n")
    fake_meta = type("Meta", (), {"project": "p", "title": "t", "version": "v1"})()
    monkeypatch.setattr(pipeline, "parse", lambda _p: (fake_meta, ["sections"]))
    monkeypatch.setattr(pipeline, "chunk_sections", lambda _s: ["chunk"])

    def fake_upsert(meta, chunks, doc_sha):
        raise pipeline.ShrinkRejected("would drop 3 requirements")

    monkeypatch.setattr(pipeline, "upsert", fake_upsert)
    with pytest.raises(SystemExit, match="refused"):
        pipeline.main(["prog", path])


def test_markdown_refused_on_real_db(tmp_path, monkeypatch):
    """store.upsert refuses outright against a real (ingestion-v2) database — the CLI
    surfaces that as a clean SystemExit, not a traceback. (The real DB check lives in
    store.upsert; here it's monkeypatched the same way ShrinkRejected is above.)"""
    path = _write(tmp_path, "brd.md", b"# Title\n")
    fake_meta = type("Meta", (), {"project": "p", "title": "t", "version": "v1"})()
    monkeypatch.setattr(pipeline, "parse", lambda _p: (fake_meta, ["sections"]))
    monkeypatch.setattr(pipeline, "chunk_sections", lambda _s: ["chunk"])

    def fake_upsert(meta, chunks, doc_sha):
        raise pipeline.WrongDatabase("The Markdown path is only for the demo DB; upload .pdf/.docx instead.")

    monkeypatch.setattr(pipeline, "upsert", fake_upsert)
    with pytest.raises(SystemExit, match="only for the demo DB"):
        pipeline.main(["prog", path])


# --- --wait: terminal statuses + "still queued" hint --------------------------------------

def test_wait_treats_canceled_as_terminal(tmp_path, monkeypatch):
    path = _write(tmp_path, "brd.pdf", b"stub")
    monkeypatch.setattr(real_jobs, "submit", lambda *a, **k: {
        "ok": True, "job_id": 9, "project": "p", "title": "t", "mode": "new", "status": "queued"})
    monkeypatch.setattr(real_jobs, "jobs", lambda *a, **k: [
        {"id": 9, "project": "p", "status": "canceled", "stage": "embed", "done": 1, "total": 3,
         "error": None, "warnings": []}])
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_a: None)

    pipeline.main(["prog", path, "--owner-id", "6", "--wait"])  # terminal — must not time out


def test_wait_warns_when_still_queued_after_60s(tmp_path, monkeypatch, capsys):
    path = _write(tmp_path, "brd.pdf", b"stub")
    monkeypatch.setattr(real_jobs, "submit", lambda *a, **k: {
        "ok": True, "job_id": 11, "project": "p", "title": "t", "mode": "new", "status": "queued"})

    responses = iter([
        [{"id": 11, "project": "p", "status": "queued", "stage": "queued", "done": 0, "total": None,
          "error": None, "warnings": []}],
        [{"id": 11, "project": "p", "status": "done", "stage": "store", "done": 1, "total": 1,
          "error": None, "warnings": []}],
    ])
    monkeypatch.setattr(real_jobs, "jobs", lambda *a, **k: next(responses))
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_a: None)

    clock = iter([0.0, 61.0, 61.0, 62.0])
    monkeypatch.setattr(pipeline.time, "time", lambda: next(clock, 9999.0))

    pipeline.main(["prog", path, "--owner-id", "6", "--wait"])
    assert "Still queued" in capsys.readouterr().out

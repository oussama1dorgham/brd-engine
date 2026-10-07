"""Pure-logic / DB-faked tests for the ingest worker's claim-token fencing (jobs.py).

_update / _check_live / _fail all guard their writes with
`where id = %s and status = 'running' and attempts = %s` — the `attempts` value _claim()
handed back doubles as a lease/claim token. These tests fake the pool (no DB, no network) and
assert the fencing behavior directly: a write whose WHERE clause doesn't match (rowcount 0,
simulating a reclaimed or canceled job) is a safe no-op, never touching brd_document.
"""
import pytest

import backend.ingest.jobs as jobs


class _Cur:
    def __init__(self, rowcount: int = 0, fetchone_result=None):
        self.rowcount = rowcount
        self._fetchone = fetchone_result
        self.executed: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone


class _Conn:
    def __init__(self, cur):
        self._c = cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self._c

    def commit(self):
        pass


class _Pool:
    def __init__(self, cur):
        self._c = cur

    def connection(self):
        return _Conn(self._c)


# --- _update: fenced write ---------------------------------------------------------------

def test_update_writes_when_claim_held(monkeypatch):
    cur = _Cur(rowcount=1)
    monkeypatch.setattr(jobs, "pool", lambda: _Pool(cur))
    assert jobs._update({"id": 7, "attempts": 3}, status="done") is True
    sql, params = cur.executed[0]
    assert "status = 'running'" in sql and "attempts = %s" in sql
    assert params[-2:] == (7, 3)          # job id + claim token are the last two bind params


def test_update_noop_when_claim_lost(monkeypatch):
    cur = _Cur(rowcount=0)                # another worker reclaimed it, or it was canceled
    monkeypatch.setattr(jobs, "pool", lambda: _Pool(cur))
    assert jobs._update({"id": 7, "attempts": 3}, status="done") is False


# --- _check_live: fenced heartbeat --------------------------------------------------------

def test_check_live_raises_canceled_when_not_alive(monkeypatch):
    cur = _Cur(fetchone_result=None)      # update matched 0 rows -> no id returned
    monkeypatch.setattr(jobs, "pool", lambda: _Pool(cur))
    with pytest.raises(jobs.Canceled):
        jobs._check_live({"id": 1, "attempts": 2})


def test_check_live_ok_when_alive(monkeypatch):
    cur = _Cur(fetchone_result=(1,))
    monkeypatch.setattr(jobs, "pool", lambda: _Pool(cur))
    jobs._check_live({"id": 1, "attempts": 2})     # must not raise


# --- _fail: both branches fenced; brd_document touched only on a real write --------------

def test_fail_retry_path_never_touches_brd_document(monkeypatch):
    cur = _Cur(rowcount=1)
    monkeypatch.setattr(jobs, "pool", lambda: _Pool(cur))
    calls = []
    monkeypatch.setattr(jobs, "_mark_doc_failed", lambda *a: calls.append(a))
    jobs._fail({"id": 1, "attempts": 1, "max_attempts": 4}, "boom", {}, retry=True)
    assert not calls


def test_fail_terminal_path_skips_mark_doc_failed_when_claim_lost(monkeypatch):
    cur = _Cur(rowcount=0)                # lost the claim: canceled or reclaimed meanwhile
    monkeypatch.setattr(jobs, "pool", lambda: _Pool(cur))
    calls = []
    monkeypatch.setattr(jobs, "_mark_doc_failed", lambda *a: calls.append(a))
    jobs._fail({"id": 1, "attempts": 5, "max_attempts": 4}, "boom", {}, retry=True)
    assert not calls          # previously: unconditional UPDATE could clobber 'canceled'


def test_fail_terminal_path_marks_doc_failed_when_claim_held(monkeypatch):
    cur = _Cur(rowcount=1)
    monkeypatch.setattr(jobs, "pool", lambda: _Pool(cur))
    calls = []
    monkeypatch.setattr(jobs, "_mark_doc_failed", lambda *a: calls.append(a))
    jobs._fail({"id": 1, "attempts": 5, "max_attempts": 4}, "boom", {}, retry=True)
    assert calls


# --- _job_fence: the callable passed to revise.compare's `fence` hook, and used directly
# in _store_new, to re-check (under FOR UPDATE, in the CALLER's transaction) that the job is
# still 'running' with the claimed `attempts` token before any write happens -------------

def test_job_fence_raises_canceled_when_claim_not_held():
    cur = _Cur(fetchone_result=None)      # 0 rows matched: reclaimed or canceled
    with pytest.raises(jobs.Canceled):
        jobs._job_fence({"id": 9, "attempts": 2})(cur)


def test_job_fence_ok_when_claim_held():
    cur = _Cur(fetchone_result=(1,))
    jobs._job_fence({"id": 9, "attempts": 2})(cur)     # must not raise, writes nothing itself
    sql, params = cur.executed[-1]
    assert "for update" in sql.lower() and "status = 'running'" in sql
    assert params == (9, 2)


# --- embed_input_text (chunker) used by both ingestion and requirement edits -------------

def test_embed_input_text_matches_embed_input_via_chunk():
    from backend.ingest.chunker import embed_input, embed_input_text
    from backend.ingest.models import Chunk

    c = Chunk(ordinal=0, section="Scope", req_id="FR-1", text="Do the thing.",
              token_count=4, content_hash="h", section_path="Scope / Sub")
    assert embed_input("My BRD", c) == embed_input_text("My BRD", c.section_path, c.section, c.text)

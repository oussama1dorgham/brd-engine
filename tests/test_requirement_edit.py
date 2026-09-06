"""Requirement-edit guards (pure logic; DB faked). The full re-embed/re-index/
cache-bust/audit cycle is covered by a live integration run."""
import pytest

import backend.ingest.edit as edit


class _Cur:
    def __init__(self, result): self.result = result
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): pass
    def fetchone(self): return self.result


class _Conn:
    def __init__(self, cur): self._c = cur
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return self._c


class _Pool:
    def __init__(self, cur): self._c = cur
    def connection(self): return _Conn(self._c)


def test_empty_text_raises_before_db(monkeypatch):
    def boom(): raise AssertionError("must not touch the DB")
    monkeypatch.setattr(edit, "pool", boom)
    with pytest.raises(ValueError):
        edit.update_requirement(1, 1, "   ")


def test_unknown_or_unowned_chunk_raises(monkeypatch):
    monkeypatch.setattr(edit, "pool", lambda: _Pool(_Cur(None)))   # lookup returns nothing
    # must not try to embed if the chunk isn't the user's
    monkeypatch.setattr(edit, "embed_documents", lambda t: (_ for _ in ()).throw(AssertionError("no embed")))
    with pytest.raises(edit.ChunkNotFound):
        edit.update_requirement(1, 999, "some new requirement text")

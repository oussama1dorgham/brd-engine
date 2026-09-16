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


# --- apply_change_set: atomic-batch guards (all validated BEFORE any DB/embed) ---

def _no_db(monkeypatch):
    """Assert the batch never touches the pool or embeds during pure validation."""
    monkeypatch.setattr(edit, "pool", lambda: (_ for _ in ()).throw(AssertionError("no DB")))
    monkeypatch.setattr(edit, "embed_documents", lambda t: (_ for _ in ()).throw(AssertionError("no embed")))


def test_apply_change_set_empty_is_noop(monkeypatch):
    _no_db(monkeypatch)
    res = edit.apply_change_set(1, "proj", [])
    assert res == {"applied": 0, "edited": 0, "added": 0, "deleted": 0, "results": []}


def test_apply_change_set_op_cap(monkeypatch):
    _no_db(monkeypatch)
    ops = [{"op": "delete", "chunk_id": i} for i in range(edit.MAX_BATCH_OPS + 1)]
    with pytest.raises(ValueError):
        edit.apply_change_set(1, "proj", ops)


def test_apply_change_set_rejects_bad_ops(monkeypatch):
    _no_db(monkeypatch)
    for bad in ([{"op": "frobnicate"}],
                [{"op": "edit", "new_text": "x"}],            # missing chunk_id
                [{"op": "edit", "chunk_id": 1, "new_text": "  "}],  # empty text
                [{"op": "add", "new_text": ""}],              # empty text
                [{"op": "delete"}]):                          # missing chunk_id
        with pytest.raises(ValueError):
            edit.apply_change_set(1, "proj", bad)


def test_derive_key_is_stable_and_specific():
    ops = [{"op": "edit", "chunk_id": 1, "old_text": "a", "new_text": "b"}]
    # deterministic + order-insensitive within a dict, and prefixed
    assert edit._derive_key("proj", ops) == edit._derive_key("proj", ops)
    assert edit._derive_key("proj", ops).startswith("auto:")
    # a different plan, project, or even one changed field ⇒ a different key
    assert edit._derive_key("proj", ops) != edit._derive_key("other", ops)
    ops2 = [{"op": "edit", "chunk_id": 1, "old_text": "a", "new_text": "c"}]
    assert edit._derive_key("proj", ops) != edit._derive_key("proj", ops2)

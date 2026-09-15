"""Pure-logic tests for the deterministic batching that incremental `sync` relies on.

The whole incremental-regeneration design rests on one property: a batch's hash is a
pure function of its requirement (chunk_id, text) pairs, so an edit re-hashes ONLY the
batch(es) it touches and every other batch's hash is byte-identical across runs. These
tests pin that without a database.
"""
from backend.generate.use_cases import _batches, _group_by_scope, _reqs_hash, _SCOPE_BATCH


def _reqs(section: str, n: int, start: int = 1, text=lambda i: f"requirement text {i}"):
    return [{"chunk_id": i, "req_id": f"R{i}", "section": section, "ordinal": i, "text": text(i)}
            for i in range(start, start + n)]


def test_reqs_hash_is_stable_and_content_sensitive():
    items = _reqs("A", 3)
    assert _reqs_hash(items) == _reqs_hash(items)                 # stable
    changed = [{**items[0], "text": "different"}, items[1], items[2]]
    assert _reqs_hash(changed) != _reqs_hash(items)              # text-sensitive
    # chunk_id is part of the identity too
    reid = [{**items[0], "chunk_id": 999}, items[1], items[2]]
    assert _reqs_hash(reid) != _reqs_hash(items)


def test_group_by_scope_preserves_first_seen_order_and_defaults():
    reqs = [{"chunk_id": 1, "section": "B", "text": "x"},
            {"chunk_id": 2, "section": None, "text": "y"},   # -> "General"
            {"chunk_id": 3, "section": "B", "text": "z"}]
    groups = _group_by_scope(reqs)
    assert [scope for scope, _ in groups] == ["B", "General"]
    assert [it["chunk_id"] for it in dict(groups)["B"]] == [1, 3]


def test_batches_are_deterministic():
    reqs = _reqs("A", 20) + _reqs("B", 10, start=100)
    assert _batches(reqs) == _batches(reqs)


def test_inplace_edit_rehashes_only_its_own_batch():
    # 20 reqs in A (spills into 2 batches of 15/5) + 10 in B (1 batch)
    reqs = _reqs("A", 20) + _reqs("B", 10, start=100)
    before = {(scope, i): h for i, (scope, _, h) in enumerate(_batches(reqs))}

    # edit one requirement in A's FIRST batch, in place (no reorder)
    edited = [dict(r) for r in reqs]
    edited[3]["text"] = "EDITED requirement"
    after = {(scope, i): h for i, (scope, _, h) in enumerate(_batches(edited))}

    changed = [k for k in before if before[k] != after[k]]
    assert len(changed) == 1                      # exactly one batch re-hashed
    assert changed[0][0] == "A"                   # and it's in scope A
    # scope B batches are untouched
    assert all(before[k] == after[k] for k in before if k[0] == "B")


def test_remove_confined_to_its_scope():
    reqs = _reqs("A", 20) + _reqs("B", 10, start=100)
    before = [(scope, h) for scope, _, h in _batches(reqs)]
    reduced = [r for r in reqs if r["chunk_id"] != 5]     # drop one req from A
    after = [(scope, h) for scope, _, h in _batches(reduced)]

    b_before = [h for s, h in before if s == "B"]
    b_after = [h for s, h in after if s == "B"]
    assert b_before == b_after                    # other scope entirely unaffected


def test_batch_size_bounds_slices():
    reqs = _reqs("A", _SCOPE_BATCH * 2 + 3)
    sizes = [len(items) for _, items, _ in _batches(reqs)]
    assert sizes == [_SCOPE_BATCH, _SCOPE_BATCH, 3]

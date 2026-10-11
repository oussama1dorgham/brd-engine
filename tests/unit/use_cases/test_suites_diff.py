"""suites.diff_rows / fingerprint — pure comparison of two suite sides (no DB)."""
from backend.use_cases.suites import diff_rows, fingerprint


def _r(uid, title="T", steps=("a",), folder=("F",), ordinal=0, req=None, def_hash=None, content=None):
    data = {"uc_id": f"UC-{uid}", "title": title, "steps": list(steps), "case_type": "functional"}
    req = req or {"1": "h1"}
    content = content or f"{title}|{steps}"
    return {"uid": uid, "ordinal": ordinal, "folder_path": list(folder), "data": data,
            "def_hash": def_hash or f"d{steps}", "content_hash": content, "req_hashes": req,
            "basis_hash": f"{content}|{sorted(req.items())}"}


def _change(d, uid):
    return next(i for i in d["items"] if i["uid"] == uid)


def test_unchanged_sides_have_no_changes():
    a = {1: _r(1), 2: _r(2)}
    d = diff_rows(a, dict(a))
    assert not d["summary"]["has_changes"] and d["items"] == [] and d["summary"]["unchanged"] == 2


def test_added_removed():
    d = diff_rows({1: _r(1)}, {2: _r(2)})
    assert _change(d, 2)["change"] == "added" and _change(d, 1)["change"] == "removed"
    assert d["summary"]["added"] == d["summary"]["removed"] == 1


def test_title_edit_is_modified_without_retest():
    d = diff_rows({1: _r(1)}, {1: _r(1, title="T2", def_hash="d('a',)")})
    i = _change(d, 1)
    assert i["change"] == "modified" and i["fields"] == ["title"] and not i["retest"]
    assert i["before"]["title"] == "T" and i["after"]["title"] == "T2"


def test_steps_edit_needs_retest():
    i = _change(diff_rows({1: _r(1)}, {1: _r(1, steps=("a", "b"))}), 1)
    assert i["change"] == "modified" and "steps" in i["fields"] and i["retest"]


def test_requirement_changed_under_unchanged_case():
    i = _change(diff_rows({1: _r(1)}, {1: _r(1, req={"1": "h1-new"})}), 1)
    assert i["change"] == "requirement_changed" and i["requirements"] == ["1"] and i["retest"]


def test_move_and_reorder_are_not_retests():
    d = diff_rows({1: _r(1), 2: _r(2)}, {1: _r(1, folder=("G",)), 2: _r(2, ordinal=5)})
    assert _change(d, 1)["change"] == "moved" and _change(d, 2)["change"] == "reordered"
    assert d["summary"]["retest"] == 0 and d["summary"]["has_changes"]


def test_modified_and_moved_flags_both():
    i = _change(diff_rows({1: _r(1)}, {1: _r(1, title="X", folder=("G",))}), 1)
    assert i["change"] == "modified" and i["moved"]


def test_fingerprint_tracks_publishable_state_only():
    a = {1: _r(1), 2: _r(2)}
    assert fingerprint(a) == fingerprint({2: _r(2), 1: _r(1)})             # order-independent
    assert fingerprint(a) != fingerprint({1: _r(1, title="X"), 2: _r(2)})  # content
    assert fingerprint(a) != fingerprint({1: _r(1, folder=("G",)), 2: _r(2)})  # placement
    assert fingerprint(a) != fingerprint({1: _r(1)})                       # membership

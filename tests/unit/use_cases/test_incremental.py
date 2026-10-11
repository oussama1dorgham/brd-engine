"""incremental.plan / units / parse_update — requirement-level change detection (pure)."""
from backend.use_cases import incremental as inc


def _req(cid, text, section="Pay"):
    return {"chunk_id": cid, "text": text, "section": section}


def _card(i, cids, **kw):
    return {"id": i, "uid": 100 + i, "title": f"Case {i}", "source_chunk_ids": cids, **kw}


REQS = [_req(1, "card"), _req(2, "refund"), _req(3, "invoice", "Bill")]
BASIS = {r["chunk_id"]: inc.req_hash(r["text"]) for r in REQS}


def test_nothing_pending_when_basis_matches():
    p = inc.plan(REQS, BASIS, [_card(1, [1]), _card(2, [3])])
    assert not p["pending"] and not p["removed_ids"] and not p["affected"] and not p["orphans"]
    assert inc.units(p, REQS, 15) == []


def test_changed_new_removed_detected_per_requirement():
    reqs = [_req(1, "card v2"), _req(3, "invoice", "Bill"), _req(4, "wallet")]   # 1 changed, 2 removed, 4 new
    p = inc.plan(reqs, BASIS, [_card(1, [1]), _card(2, [2]), _card(3, [2, 3]), _card(4, [3]), _card(5, [])])
    assert p["changed_ids"] == {1} and p["new_ids"] == {4} and p["removed_ids"] == {2}
    assert [c["id"] for c in p["orphans"]] == [2]                     # cites only the removed requirement
    assert {c["id"] for c in p["affected"]} == {1, 3}                  # cites a changed / removed one
    # card 4 (unchanged requirement) and card 5 (manual, no citations) are untouched


def test_first_generation_everything_is_new():
    p = inc.plan(REQS, {}, [])
    assert p["new_ids"] == {1, 2, 3} and not p["changed_ids"]
    us = inc.units(p, REQS, 15)
    assert [(u["scope"], [r["chunk_id"] for r in u["items"]], u["cards"]) for u in us] == \
        [("Pay", [1, 2], []), ("Bill", [3], [])]


def test_units_batch_and_place_cards_with_their_requirement():
    reqs = [_req(i, f"t{i}") for i in range(1, 6)]
    p = inc.plan(reqs, {}, [_card(1, [4]), _card(2, [1, 5])])
    us = inc.units(p, reqs, 2)
    assert [[r["chunk_id"] for r in u["items"]] for u in us] == [[1, 2], [3, 4], [5]]
    assert [c["id"] for c in us[1]["cards"]] == [1] and [c["id"] for c in us[0]["cards"]] == [2]
    assert [r["chunk_id"] for r in us[0]["context"]] == [5]            # its other citation, as context


def test_removal_only_scope_still_gets_a_unit_with_context():
    reqs = [_req(1, "card"), _req(3, "invoice", "Bill")]               # requirement 2 removed
    p = inc.plan(reqs, BASIS, [_card(1, [1, 2])])
    us = inc.units(p, reqs, 15)
    assert len(us) == 1 and us[0]["items"] == [] and [c["id"] for c in us[0]["cards"]] == [1]
    assert [r["chunk_id"] for r in us[0]["context"]] == [1]


def test_parse_update_keeps_unanswered_and_invalid():
    d = {"existing": [{"ref": "E1", "action": "update", "use_case": {"title": "x"}},
                      {"ref": "E2", "action": "remove"},
                      {"ref": "E3", "action": "update"},                # no body → keep
                      {"ref": "E9", "action": "remove"},                # unknown ref → ignored
                      {"ref": "E2", "action": "keep"}],                 # duplicate → first wins
         "new": [{"title": "n"}]}
    decided, new = inc.parse_update(d, 4)
    assert decided[0] == ("update", {"title": "x"}) and decided[1] == ("remove", None)
    assert decided[2] == ("keep", None) and decided[3] == ("keep", None)   # E4 never answered → kept
    assert new == [{"title": "n"}]


def test_req_hash_matches_postgres_md5():
    assert inc.req_hash("abc") == "900150983cd24fb0d6963f7d28e17f72"
    assert inc.req_hash("الرقم") == inc.req_hash("الرقم") and len(inc.req_hash("")) == 32


def test_cache_key_changes_with_inputs():
    u = dict(scope="Pay", items=[_req(1, "a")], context=[], removed=[], cards=[_card(1, [1])])
    k = inc.cache_key(**u)
    assert k == inc.cache_key(**u)
    assert k != inc.cache_key(**{**u, "items": [_req(1, "b")]})
    assert k != inc.cache_key(**{**u, "cards": [_card(1, [1], steps=["s"])]})

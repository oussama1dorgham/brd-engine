"""Golden-set staleness guard (pure logic + file parsing, no DB/network).

A revision/edit that archives a requirement, or a BRD that gets deleted, can leave a
golden file's `expect_chunk_ids` pointing at chunks that no longer live. These tests
exercise `find_stale_expectations` (pure: given fake live/archived id sets) and
`_collect_expected_chunk_ids` (file parsing only) directly — no Postgres involved.
"""
from __future__ import annotations

import json

from backend.eval.run import _collect_expected_chunk_ids, find_stale_expectations


# --- find_stale_expectations (pure) ------------------------------------------------------

def test_all_live_is_clean():
    report = find_stale_expectations({"a.json": [1, 2, 3]}, live_ids={1, 2, 3}, archived_ids=set())
    assert report == {}


def test_archived_id_is_flagged():
    report = find_stale_expectations({"a.json": [1, 2]}, live_ids={1}, archived_ids={2})
    assert report == {"a.json": {"archived": [2], "missing": []}}


def test_missing_id_is_flagged():
    # neither live nor archived (e.g. the whole BRD was deleted)
    report = find_stale_expectations({"a.json": [1, 99]}, live_ids={1}, archived_ids=set())
    assert report == {"a.json": {"archived": [], "missing": [99]}}


def test_mixed_archived_and_missing_in_one_file():
    report = find_stale_expectations(
        {"a.json": [1, 2, 3, 4]}, live_ids={1}, archived_ids={2, 3},
    )
    assert report == {"a.json": {"archived": [2, 3], "missing": [4]}}


def test_clean_file_omitted_from_multi_file_report():
    report = find_stale_expectations(
        {"clean.json": [1], "stale.json": [1, 2]}, live_ids={1}, archived_ids={2},
    )
    assert set(report) == {"stale.json"}
    assert report["stale.json"] == {"archived": [2], "missing": []}


def test_empty_expectations_is_clean():
    report = find_stale_expectations({"a.json": []}, live_ids=set(), archived_ids=set())
    assert report == {}


def test_results_are_sorted_and_deduped():
    report = find_stale_expectations({"a.json": [5, 2, 2, 9]}, live_ids=set(), archived_ids={9, 2})
    assert report["a.json"]["archived"] == [2, 9]


# --- _collect_expected_chunk_ids (file parsing only) --------------------------------------

def _write_golden(tmp_path, name: str, cases: list[dict]) -> None:
    (tmp_path / name).write_text(json.dumps({"owner_id": 1, "project": "p", "cases": cases}),
                                  encoding="utf-8")


def test_collect_reads_expect_chunk_ids_across_cases(tmp_path):
    _write_golden(tmp_path, "a.json", [
        {"question": "q1", "expect_chunk_ids": [3, 1]},
        {"question": "q2", "expect_chunk_ids": [1, 2]},
    ])
    out = _collect_expected_chunk_ids(tmp_path)
    assert out == {"a.json": [1, 2, 3]}


def test_collect_skips_abstain_and_legacy_req_id_only_cases(tmp_path):
    _write_golden(tmp_path, "a.json", [
        {"question": "abstain", "expect_chunk_ids": []},
        {"question": "legacy", "expect_req_ids": ["FR-1"]},
    ])
    out = _collect_expected_chunk_ids(tmp_path)
    assert out == {}


def test_collect_spans_multiple_files(tmp_path):
    _write_golden(tmp_path, "a.json", [{"question": "q", "expect_chunk_ids": [1]}])
    _write_golden(tmp_path, "b.json", [{"question": "q", "expect_chunk_ids": [2]}])
    out = _collect_expected_chunk_ids(tmp_path)
    assert out == {"a.json": [1], "b.json": [2]}

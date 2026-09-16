"""Unit tests for the RAG eval metrics (pure logic, no DB/network)."""
from math import log2

import pytest

from backend.eval.metrics import (
    abstained_correct, hit_at_k, ndcg_at_k, precision_at_k, recall_at_k, reciprocal_rank,
)

RANKED = [10, 20, 30, 40, 50]   # retrieved chunk ids, best first


def test_hit_at_k():
    assert hit_at_k(RANKED, {30}, 3) == 1.0
    assert hit_at_k(RANKED, {30}, 2) == 0.0        # 30 is at rank 3
    assert hit_at_k(RANKED, set(), 5) == 0.0        # no relevant → not a hit


def test_recall_at_k():
    assert recall_at_k(RANKED, {10, 40}, 5) == 1.0
    assert recall_at_k(RANKED, {10, 40}, 2) == 0.5  # only 10 in top 2
    assert recall_at_k(RANKED, {99}, 5) == 0.0
    assert recall_at_k(RANKED, set(), 5) == 0.0


def test_precision_at_k():
    assert precision_at_k(RANKED, {10, 20}, 2) == 1.0
    assert precision_at_k(RANKED, {10}, 2) == 0.5
    assert precision_at_k([], {10}, 3) == 0.0
    # short list isn't penalised: 1 relevant of 2 returned when k=5
    assert precision_at_k([10, 99], {10}, 5) == 0.5


def test_reciprocal_rank():
    assert reciprocal_rank(RANKED, {10}) == 1.0
    assert reciprocal_rank(RANKED, {30}) == pytest.approx(1 / 3)
    assert reciprocal_rank(RANKED, {99}) == 0.0


def test_ndcg_at_k():
    assert ndcg_at_k(RANKED, {10}, 5) == pytest.approx(1.0)   # single relevant at top → perfect
    assert ndcg_at_k(RANKED, set(), 5) == 0.0
    # relevant at rank 3 only: DCG = 1/log2(4); IDCG = 1/log2(2)=1 → nDCG = 1/log2(4)
    assert ndcg_at_k(RANKED, {30}, 5) == pytest.approx(1.0 / log2(4))
    # two relevant, both retrieved but at ranks 1 and 3; ideal has them at ranks 1,2
    dcg = 1.0 / log2(2) + 1.0 / log2(4)
    idcg = 1.0 / log2(2) + 1.0 / log2(3)
    assert ndcg_at_k(RANKED, {10, 30}, 5) == pytest.approx(dcg / idcg)


def test_abstained_correct():
    # should abstain and did (low score) → correct
    assert abstained_correct(0.1, 0.3, True) is True
    # should abstain but didn't (high score) → wrong
    assert abstained_correct(0.9, 0.3, True) is False
    # should answer and did (high score) → correct
    assert abstained_correct(0.9, 0.3, False) is True
    # nothing retrieved counts as abstained
    assert abstained_correct(None, 0.3, True) is True
    assert abstained_correct(None, 0.3, False) is False

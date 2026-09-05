"""Reciprocal Rank Fusion math in the retriever."""
from backend.retrieve.retriever import _rrf


def test_agreement_ranks_first():
    # id 10 is top of both lists -> should win
    scores = _rrf([[10, 20, 30], [10, 40, 50]])
    ranked = sorted(scores, key=lambda cid: scores[cid], reverse=True)
    assert ranked[0] == 10


def test_single_list_orders_by_rank():
    scores = _rrf([[7, 8, 9]])
    assert scores[7] > scores[8] > scores[9]


def test_default_k_value():
    # rank 0 with default k=60 -> 1/(60+0+1)
    scores = _rrf([[42]])
    assert abs(scores[42] - 1 / 61) < 1e-9


def test_missing_ids_absent():
    scores = _rrf([[1, 2]])
    assert 3 not in scores

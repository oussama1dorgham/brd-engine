"""Pure information-retrieval metrics for the RAG eval harness.

Zero-cost, no DB, no network — just functions over a ranked list of chunk ids and the set
of relevant chunk ids for a query. Used by backend/eval/run.py to score each retrieval
stage (vector / keyword / fused / +rerank / +transform) against Golden Set v2.

Ranked ids are the retrieved chunk ids in rank order (best first); `relevant` is the set
of chunk ids that should have been retrieved for the query. Binary relevance throughout.
"""
from __future__ import annotations

from math import log2


def hit_at_k(ranked: list[int], relevant: set[int], k: int) -> float:
    """1.0 if any relevant id appears in the top k, else 0.0."""
    if not relevant:
        return 0.0
    return 1.0 if relevant & set(ranked[:k]) else 0.0


def recall_at_k(ranked: list[int], relevant: set[int], k: int) -> float:
    """Fraction of the relevant ids that appear in the top k."""
    if not relevant:
        return 0.0
    return len(relevant & set(ranked[:k])) / len(relevant)


def precision_at_k(ranked: list[int], relevant: set[int], k: int) -> float:
    """Fraction of the top k that are relevant (a.k.a. context precision@k).
    Divided by the number actually returned (min(k, len)), so a short list isn't penalised."""
    top = ranked[:k]
    if not top:
        return 0.0
    return len(relevant & set(top)) / len(top)


def reciprocal_rank(ranked: list[int], relevant: set[int]) -> float:
    """1 / (rank of the first relevant id), 1-indexed; 0.0 if none is retrieved."""
    for i, cid in enumerate(ranked):
        if cid in relevant:
            return 1.0 / (i + 1)
    return 0.0


def ndcg_at_k(ranked: list[int], relevant: set[int], k: int) -> float:
    """Normalised discounted cumulative gain at k (binary relevance).

    DCG rewards relevant ids near the top (gain 1, discounted by 1/log2(rank+1)); IDCG is
    the best achievable given how many relevant ids exist. nDCG = DCG / IDCG in [0, 1]."""
    if not relevant:
        return 0.0
    dcg = sum(1.0 / log2(i + 2) for i, cid in enumerate(ranked[:k]) if cid in relevant)
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / log2(i + 2) for i in range(ideal_hits))
    return dcg / idcg if idcg else 0.0


def abstained_correct(top_score: float | None, threshold: float, should_abstain: bool) -> bool:
    """Abstention accuracy for one case. `should_abstain` is True for 'not specified' golden
    cases (no relevant chunk). A correct outcome is: abstain (top_score below threshold or
    nothing retrieved) exactly when we should. Returns True when the outcome is correct."""
    abstained = top_score is None or top_score < threshold
    return abstained == should_abstain

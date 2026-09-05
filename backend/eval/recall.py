"""Retrieval eval against the golden set — the Phase 2 exit test.

For each ID-bearing question, retrieve top-k and check whether the expected
requirement appears. Reports recall@k and MRR, with rerank ON vs OFF so the
reranker's contribution is measurable. Questions with no expected IDs (the
"not specified" cases) are listed separately — their real test is abstention
at answer time (Phase 4).

All query embeddings are computed once, up front, in a single batched call.

    python -m backend.eval.recall
    python -m backend.eval.recall --k 3
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import mean

from ..ingest.embedder import embed_queries
from ..retrieve.retriever import retrieve

GOLDEN = Path("data/golden_qa.json")


def evaluate(cases: list[dict], k: int, use_rerank: bool, vecs: dict[str, list[float]]):
    recalls, rr, detail = [], [], []
    for c in cases:
        res = retrieve(c["question"], k_final=k, project=c.get("project"),
                       use_rerank=use_rerank, query_vec=vecs[c["question"]])
        got = [r["req_id"] for r in res if r["req_id"]]
        expected = set(c["expect_req_ids"])
        found = expected & set(got)
        recalls.append(len(found) / len(expected))
        rank = next((i + 1 for i, rq in enumerate(got) if rq in expected), None)
        rr.append(1.0 / rank if rank else 0.0)
        detail.append((c["question"], expected, rank))
    return mean(recalls), mean(rr), detail


def main(argv: list[str]) -> None:
    k = 5
    if "--k" in argv:
        k = int(argv[argv.index("--k") + 1])

    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = data["cases"]
    id_cases = [c for c in cases if c["expect_req_ids"]]
    ns_cases = [c for c in cases if not c["expect_req_ids"]]

    # one batched embedding call for every question
    questions = [c["question"] for c in cases]
    qvecs, _ = embed_queries(questions)
    vecs = dict(zip(questions, qvecs))

    print(f"golden cases: {len(id_cases)} with expected IDs, {len(ns_cases)} 'not-specified'\n")

    r_off, mrr_off, _ = evaluate(id_cases, k, False, vecs)
    r_on, mrr_on, detail = evaluate(id_cases, k, True, vecs)

    print(f"{'mode':<18}{'recall@'+str(k):>10}{'MRR':>8}")
    print("-" * 36)
    print(f"{'hybrid (RRF)':<18}{r_off:>10.3f}{mrr_off:>8.3f}")
    print(f"{'hybrid + rerank':<18}{r_on:>10.3f}{mrr_on:>8.3f}")

    print(f"\nper-question (hybrid + rerank, k={k}):")
    print(f"{'expected':<9}{'rank':>5}   question")
    print("-" * 74)
    for q, expected, rank in detail:
        tag = f"#{rank}" if rank else "MISS"
        print(f"{','.join(expected):<9}{tag:>5}   {q[:54]}")

    print("\nnot-specified cases (top hit should be weak / off-topic):")
    for c in ns_cases:
        res = retrieve(c["question"], k_final=1, project=c.get("project"),
                       use_rerank=True, query_vec=vecs[c["question"]])
        top = res[0] if res else None
        shown = f"{top['req_id'] or '-'} (score {top['score']:.3f})" if top else "nothing"
        print(f"  - {c['question'][:52]:<52} top: {shown}")


if __name__ == "__main__":
    main(sys.argv)

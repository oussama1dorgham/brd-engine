"""RAG eval harness — the automated 'measure before you trust' flow.

Loads chunk_id-keyed Golden Set v2 files (data/golden/*.json), retrieves each question
through the real pipeline exposing every STAGE (vector / keyword / fused / +rerank), and
scores each stage with recall@k, MRR, nDCG@k and context precision, plus abstention
accuracy on the 'not specified' cases. Query embeddings are computed once, batched and
cached, and a single embed is reused across all stages — so a full run costs no more than
one normal retrieval per case and needs no resource upgrade.

    python -m backend.eval.run                         # all golden files, k=5
    python -m backend.eval.run --k 3 --candidates 15
    python -m backend.eval.run --report data/eval/baseline.json

Golden file shape (data/golden/<project>.json):
    { "owner_id": 6, "project": "directives-brd",
      "cases": [ {"question": "...", "expect_chunk_ids": [328], "lang": "ar", "type": "single"},
                 {"question": "...", "expect_chunk_ids": [], "type": "abstain"} ] }
`expect_req_ids` is also accepted (resolved to chunk ids) for the legacy synthetic set.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from statistics import mean

from ..db import pool
from ..ingest.embedder import embed_queries
from ..retrieve.retriever import retrieve_stages
from .metrics import abstained_correct, ndcg_at_k, precision_at_k, recall_at_k, reciprocal_rank

GOLDEN_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "golden"
STAGES = ("vector", "keyword", "fused", "rerank")


def _resolve_req_ids(owner_id: int, project: str, req_ids: list[str]) -> list[int]:
    """Map requirement ids (legacy golden sets) to chunk ids for the owner's project."""
    if not req_ids:
        return []
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "select c.id from brd_chunk c join brd_document d on d.id = c.document_id "
            "where d.owner_id = %s and d.project = %s and c.req_id = any(%s)",
            (owner_id, project, req_ids),
        )
        return [r[0] for r in cur.fetchall()]


def load_golden(directory: Path = GOLDEN_DIR) -> list[dict]:
    """Every case across all golden files, each normalised to
    {question, owner_id, project, relevant:set[int], abstain:bool, lang, type}."""
    cases: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        owner_id, project = data.get("owner_id"), data.get("project")
        for c in data.get("cases", []):
            oid = c.get("owner_id", owner_id)
            proj = c.get("project", project)
            relevant = set(c.get("expect_chunk_ids") or [])
            relevant.update(_resolve_req_ids(oid, proj, c.get("expect_req_ids") or []))
            cases.append({
                "question": c["question"], "owner_id": oid, "project": proj,
                "relevant": relevant, "abstain": len(relevant) == 0,
                "lang": c.get("lang", "?"), "type": c.get("type", "single"),
            })
    return cases


def run(k: int = 5, candidates: int = 10, threshold: float = 0.3,
        report_path: str | None = None) -> dict:
    cases = load_golden()
    if not cases:
        print(f"no golden files in {GOLDEN_DIR} — add data/golden/<project>.json "
              f"(see backend.eval.build_golden)")
        return {}
    answerable = [c for c in cases if not c["abstain"]]
    abstain_cases = [c for c in cases if c["abstain"]]

    # one batched, cached embedding call for every distinct question
    questions = list({c["question"] for c in cases})
    qvecs, _ = embed_queries(questions)
    qv = dict(zip(questions, qvecs))

    agg = {s: {"recall": [], "mrr": [], "ndcg": [], "cprec": []} for s in STAGES}
    abst_ok: list[bool] = []
    per_case: list[dict] = []
    t0 = time.time()

    for c in cases:
        st = retrieve_stages(c["question"], owner_id=c["owner_id"], project=c["project"],
                             candidates=candidates, k_final=k, query_vec=qv[c["question"]])
        ids = {"vector": st["vector"], "keyword": st["keyword"], "fused": st["fused"],
               "rerank": [cid for cid, _ in st["rerank"]]}
        top_score = st["rerank"][0][1] if st["rerank"] else None
        abst_ok.append(abstained_correct(top_score, threshold, c["abstain"]))

        row = {"question": c["question"], "lang": c["lang"], "type": c["type"],
               "abstain": c["abstain"], "top_score": top_score}
        if not c["abstain"]:
            rel = c["relevant"]
            for s in STAGES:
                agg[s]["recall"].append(recall_at_k(ids[s], rel, k))
                agg[s]["mrr"].append(reciprocal_rank(ids[s], rel))
                agg[s]["ndcg"].append(ndcg_at_k(ids[s], rel, k))
                agg[s]["cprec"].append(precision_at_k(ids[s], rel, k))
            row["rerank_rank"] = next((i + 1 for i, cid in enumerate(ids["rerank"]) if cid in rel), None)
        per_case.append(row)

    def _m(xs: list[float]) -> float:
        return mean(xs) if xs else 0.0

    summary = {
        "k": k, "candidates": candidates, "threshold": threshold,
        "cases": len(cases), "answerable": len(answerable), "abstain": len(abstain_cases),
        "elapsed_s": round(time.time() - t0, 2),
        "abstention_accuracy": _m([1.0 if x else 0.0 for x in abst_ok]),
        "stages": {s: {m: round(_m(agg[s][m]), 4) for m in ("recall", "mrr", "ndcg", "cprec")}
                   for s in STAGES},
    }

    _print(summary, per_case)
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps({"summary": summary, "cases": per_case},
                                                ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nreport → {report_path}")
    return summary


def _print(s: dict, per_case: list[dict]) -> None:
    print(f"golden: {s['cases']} cases ({s['answerable']} answerable, {s['abstain']} abstain) "
          f"· k={s['k']} candidates={s['candidates']} · {s['elapsed_s']}s\n")
    print(f"{'stage':<10}{'recall@'+str(s['k']):>10}{'MRR':>8}{'nDCG':>8}{'ctxP':>8}")
    print("-" * 44)
    for st in STAGES:
        m = s["stages"][st]
        print(f"{st:<10}{m['recall']:>10.3f}{m['mrr']:>8.3f}{m['ndcg']:>8.3f}{m['cprec']:>8.3f}")
    print(f"\nabstention accuracy: {s['abstention_accuracy']:.3f}  "
          f"(top rerank score vs threshold {s['threshold']})")


def _parse(argv: list[str]) -> dict:
    kw = {"k": 5, "candidates": 10, "threshold": 0.3, "report_path": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--k":            kw["k"] = int(argv[i + 1]); i += 2
        elif a == "--candidates": kw["candidates"] = int(argv[i + 1]); i += 2
        elif a == "--threshold":  kw["threshold"] = float(argv[i + 1]); i += 2
        elif a == "--report":     kw["report_path"] = argv[i + 1]; i += 2
        else: i += 1
    return kw


if __name__ == "__main__":
    run(**_parse(sys.argv[1:]))

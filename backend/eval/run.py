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
    python -m backend.eval.run --allow-stale            # skip the stale-chunk-id pre-flight

Golden file shape (data/golden/<project>.json):
    { "owner_id": 6, "project": "directives-brd",
      "cases": [ {"question": "...", "expect_chunk_ids": [328], "lang": "ar", "type": "single"},
                 {"question": "...", "expect_chunk_ids": [], "type": "abstain"} ] }
`expect_req_ids` is also accepted (resolved to chunk ids) for the legacy synthetic set.

Pre-flight: before scoring, every `expect_chunk_ids` entry across all golden files is
checked against the live chunks and `brd_chunk_archive` — a revision/edit that archived a
requirement, or a BRD that was deleted, otherwise silently turns that case into a miss. A
file with any archived/missing id fails the run (exit 1) unless `--allow-stale` is passed.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from statistics import mean

import psycopg

from backend.core.db import pool
from ..ingest.embedder import embed_queries
from ..retrieve.retriever import retrieve_stages
from .metrics import abstained_correct, ndcg_at_k, precision_at_k, recall_at_k, reciprocal_rank

GOLDEN_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "golden"
STAGES = ("vector", "keyword", "fused", "rerank")


class StaleGoldenSet(RuntimeError):
    """A golden file's expect_chunk_ids point at archived or missing chunks."""


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


def find_stale_expectations(golden_files: dict[str, list[int]], live_ids: set[int],
                             archived_ids: set[int]) -> dict[str, dict]:
    """Pure: given each golden file's expected chunk ids, plus the sets of currently-live
    and currently-archived chunk ids, return — per file that has a problem — which expected
    ids are archived (retired by a revision/edit) and which are missing altogether (e.g. the
    BRD itself was deleted). A file with no problems is omitted from the result."""
    report: dict[str, dict] = {}
    for fname, ids in golden_files.items():
        archived = sorted({i for i in ids if i in archived_ids})
        missing = sorted({i for i in ids if i not in live_ids and i not in archived_ids})
        if archived or missing:
            report[fname] = {"archived": archived, "missing": missing}
    return report


def _collect_expected_chunk_ids(directory: Path = GOLDEN_DIR) -> dict[str, list[int]]:
    """{golden filename: sorted expect_chunk_ids} for every file that has any — the legacy
    expect_req_ids set can't go stale the same way (it's resolved fresh each run), so it's
    left out of this check."""
    out: dict[str, list[int]] = {}
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        ids = {cid for c in data.get("cases", []) for cid in (c.get("expect_chunk_ids") or [])}
        if ids:
            out[path.name] = sorted(ids)
    return out


def check_golden_staleness(directory: Path = GOLDEN_DIR) -> dict[str, dict]:
    """DB-backed pre-flight: which golden files' expect_chunk_ids are archived or missing.
    Fails open on the archive check (the demo `brd` DB predates brd_chunk_archive)."""
    expected = _collect_expected_chunk_ids(directory)
    all_ids = sorted({i for ids in expected.values() for i in ids})
    if not all_ids:
        return {}
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select id from brd_chunk where id = any(%s)", (all_ids,))
        live_ids = {r[0] for r in cur.fetchall()}
        archived_ids: set[int] = set()
        try:
            cur.execute("select id from brd_chunk_archive where id = any(%s)", (all_ids,))
            archived_ids = {r[0] for r in cur.fetchall()}
        except psycopg.errors.UndefinedTable:
            conn.rollback()
    return find_stale_expectations(expected, live_ids, archived_ids)


def _print_stale(stale: dict[str, dict]) -> None:
    print(f"golden-set preflight: stale expect_chunk_ids in {len(stale)} file(s)\n")
    for fname, info in stale.items():
        if info["archived"]:
            print(f"  {fname}: archived -> {info['archived']}")
        if info["missing"]:
            print(f"  {fname}: missing  -> {info['missing']}")
    print()


def run(k: int = 5, candidates: int = 10, threshold: float = 0.3,
        report_path: str | None = None, allow_stale: bool = False) -> dict:
    cases = load_golden()
    if not cases:
        print(f"no golden files in {GOLDEN_DIR} — add data/golden/<project>.json "
              f"(see backend.eval.build_golden)")
        return {}

    stale = check_golden_staleness()
    if stale:
        _print_stale(stale)
        if not allow_stale:
            raise StaleGoldenSet(
                "golden set references archived/missing chunk ids (see above) — fix the golden "
                "set or rerun with --allow-stale to proceed anyway"
            )
        print("--allow-stale set — continuing despite the above\n")

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
    kw = {"k": 5, "candidates": 10, "threshold": 0.3, "report_path": None, "allow_stale": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--k":            kw["k"] = int(argv[i + 1]); i += 2
        elif a == "--candidates": kw["candidates"] = int(argv[i + 1]); i += 2
        elif a == "--threshold":  kw["threshold"] = float(argv[i + 1]); i += 2
        elif a == "--report":     kw["report_path"] = argv[i + 1]; i += 2
        elif a == "--allow-stale": kw["allow_stale"] = True; i += 1
        else: i += 1
    return kw


if __name__ == "__main__":
    try:
        run(**_parse(sys.argv[1:]))
    except StaleGoldenSet as e:
        print(f"\nFAIL — {e}")
        sys.exit(1)

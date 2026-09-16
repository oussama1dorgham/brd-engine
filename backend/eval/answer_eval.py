"""Answer-level eval — retrieval recall isn't enough to TRUST an answer.

Runs the full ChatSession per golden case (retrieve → gate → generate → grounding guard →
verify) and scores the ANSWER: is it grounded (cites a source or abstains), does it cite the
EXPECTED requirement (citation correctness), and does it abstain exactly on the 'not
specified' cases. Reuses the existing guard (refine.is_grounded) — no new judge.

Generation is rate-limited on the free tier, so this defaults to a small SUBSET; use --full
for every case (slow). Per-case results are printed and optionally written to a report.

    python -m backend.eval.answer_eval                 # subset
    python -m backend.eval.answer_eval --limit 8
    python -m backend.eval.answer_eval --full --report data/eval/answers.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from statistics import mean

from ..generate.answer import ABSTAIN
from ..generate.refine import is_grounded
from ..generate.session import ChatSession
from .run import load_golden


def evaluate(limit: int = 5, full: bool = False, report_path: str | None = None) -> dict:
    cases = load_golden()
    if not full:
        answerable = [c for c in cases if not c["abstain"]][:limit]
        abstain = [c for c in cases if c["abstain"]][:2]
        cases = answerable + abstain

    grounded, cite_hit, abst_ok = [], [], []
    rows = []
    t0 = time.time()
    for c in cases:
        sess = ChatSession(project=c["project"], owner_id=c["owner_id"], persist=False)
        try:
            res = sess.ask(c["question"])
        except Exception as e:  # noqa: BLE001
            rows.append({"q": c["question"][:60], "error": f"{type(e).__name__}: {e}"})
            continue
        answer = (res.get("answer") or "").strip()
        sources = res.get("sources") or []
        cited = {s.get("chunk_id") for s in sources if s.get("chunk_id") is not None}
        abstained = (not answer) or answer == ABSTAIN or ABSTAIN in answer or not cited
        g = is_grounded(answer, len(sources))
        grounded.append(1.0 if g else 0.0)

        row = {"q": c["question"][:60], "lang": c["lang"], "abstain_case": c["abstain"],
               "abstained": abstained, "grounded": g, "cited": sorted(cited)}
        if c["abstain"]:
            abst_ok.append(1.0 if abstained else 0.0)
        else:
            abst_ok.append(1.0 if not abstained else 0.0)
            hit = bool(c["relevant"] & cited)
            cite_hit.append(1.0 if hit else 0.0)
            row["expected"] = sorted(c["relevant"])
            row["cite_hit"] = hit
        rows.append(row)

    def _m(xs):
        return round(mean(xs), 3) if xs else None

    summary = {
        "cases": len(cases), "elapsed_s": round(time.time() - t0, 1),
        "groundedness": _m(grounded),
        "citation_hit": _m(cite_hit),
        "abstention_correct": _m(abst_ok),
    }
    print(f"answer-level eval: {summary['cases']} cases · {summary['elapsed_s']}s\n")
    print(f"  groundedness       : {summary['groundedness']}")
    print(f"  citation hit rate  : {summary['citation_hit']}   (answer cites the expected requirement)")
    print(f"  abstention correct : {summary['abstention_correct']}")
    for r in rows:
        if "error" in r:
            print(f"  ERR  {r['q']}  — {r['error']}")
        else:
            tag = "ABST" if r["abstained"] else "ANS "
            mark = "" if r["abstain_case"] else ("  cite✓" if r.get("cite_hit") else "  cite✗")
            print(f"  {tag} g={int(r['grounded'])} {r['q']}{mark}")

    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps({"summary": summary, "cases": rows},
                                                ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nreport → {report_path}")
    return summary


def _parse(argv: list[str]) -> dict:
    kw = {"limit": 5, "full": False, "report_path": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--limit":    kw["limit"] = int(argv[i + 1]); i += 2
        elif a == "--full":   kw["full"] = True; i += 1
        elif a == "--report": kw["report_path"] = argv[i + 1]; i += 2
        else: i += 1
    return kw


if __name__ == "__main__":
    evaluate(**_parse(sys.argv[1:]))

"""Calibrate the retrieval abstain threshold from a saved eval report — zero new API calls.

An early abstain gate turns a low top-rerank-score retrieval into "not specified" BEFORE
generation (saving a generation call and preventing a hallucinated answer over irrelevant
context). This sweeps candidate thresholds over the report's per-case top scores and picks
the one that best separates answerable (should answer) from 'not specified' (should abstain).

    python -m backend.eval.calibrate data/eval/phase1-arabic-fts.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def calibrate(report_path: str) -> dict:
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    cases = [c for c in data["cases"] if c.get("top_score") is not None or c["abstain"]]
    ans = [c["top_score"] for c in cases if not c["abstain"] and c.get("top_score") is not None]
    absn = [c["top_score"] for c in cases if c["abstain"] and c.get("top_score") is not None]
    n = len(ans) + len(absn)
    if n == 0:
        print("no scored cases in report")
        return {}

    # candidate thresholds = the observed scores (plus a touch below the min), so we test
    # every meaningful cut point.
    scores = sorted(set(ans + absn))
    candidates = [scores[0] - 0.01] + [(scores[i] + scores[i + 1]) / 2 for i in range(len(scores) - 1)] + [scores[-1] + 0.01]

    best = None
    curve = []
    for th in candidates:
        # answerable is correct when it scores >= th (we'd answer); abstain correct when < th
        correct = sum(1 for s in ans if s >= th) + sum(1 for s in absn if s < th)
        acc = correct / n
        curve.append((round(th, 4), round(acc, 4)))
        # prefer higher accuracy; tie-break toward a HIGHER threshold (fewer false answers)
        if best is None or acc > best[1] or (acc == best[1] and th > best[0]):
            best = (th, acc)

    th, acc = best
    # answerable that would be wrongly gated (false abstains) and abstain that would slip through
    false_abstain = sum(1 for s in ans if s < th)
    false_answer = sum(1 for s in absn if s >= th)
    print(f"cases: {n} ({len(ans)} answerable, {len(absn)} abstain)")
    print(f"answerable score range: {min(ans):.3f}–{max(ans):.3f}" if ans else "no answerable")
    print(f"abstain    score range: {min(absn):.3f}–{max(absn):.3f}" if absn else "no abstain")
    print(f"\nbest threshold: {th:.3f}  → abstention accuracy {acc:.3f}")
    print(f"  false abstains (answerable gated): {false_abstain}/{len(ans)}")
    print(f"  false answers (abstain slipped):   {false_answer}/{len(absn)}")
    return {"threshold": round(th, 3), "accuracy": round(acc, 3),
            "false_abstain": false_abstain, "false_answer": false_answer}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m backend.eval.calibrate <report.json>")
        sys.exit(2)
    calibrate(sys.argv[1])

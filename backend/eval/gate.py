"""Regression gate — fail CI when retrieval quality drops vs a stored baseline.

This is the enforcement behind 'measure before you trust': a fresh eval report is compared
to a known-good baseline, and the gate exits non-zero if any tracked metric regresses beyond
the tolerance. Wire it after `python -m backend.eval.run --report <new>` in CI.

    python -m backend.eval.gate --baseline data/eval/broad.json --check data/eval/new.json
    python -m backend.eval.gate --baseline data/eval/broad.json --check data/eval/new.json --tol 0.03
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Metrics that must not regress. (stage, metric); stage None == a top-level summary field.
GATED = [
    ("vector", "recall"), ("keyword", "recall"), ("fused", "recall"), ("rerank", "recall"),
    ("rerank", "mrr"), ("rerank", "ndcg"),
    (None, "abstention_accuracy"),
]


def _get(summary: dict, stage: str | None, metric: str) -> float:
    if stage is None:
        return float(summary.get(metric, 0.0))
    return float(summary.get("stages", {}).get(stage, {}).get(metric, 0.0))


def check(baseline_path: str, report_path: str, tol: float = 0.02) -> bool:
    base = json.loads(Path(baseline_path).read_text(encoding="utf-8"))["summary"]
    new = json.loads(Path(report_path).read_text(encoding="utf-8"))["summary"]

    print(f"gate: {report_path}  vs baseline {baseline_path}  (tol {tol})\n")
    print(f"{'metric':<28}{'base':>8}{'new':>8}{'Δ':>9}   status")
    print("-" * 62)
    ok = True
    for stage, metric in GATED:
        b, n = _get(base, stage, metric), _get(new, stage, metric)
        d = n - b
        regressed = d < -tol
        ok = ok and not regressed
        label = f"{stage + '.' if stage else ''}{metric}"
        status = "REGRESSED" if regressed else ("↑" if d > tol else "ok")
        print(f"{label:<28}{b:>8.3f}{n:>8.3f}{d:>+9.3f}   {status}")

    print("\n" + ("PASS — no regression beyond tolerance" if ok else "FAIL — a gated metric regressed"))
    return ok


def _parse(argv: list[str]) -> dict:
    kw = {"baseline_path": None, "report_path": None, "tol": 0.02}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--baseline": kw["baseline_path"] = argv[i + 1]; i += 2
        elif a == "--check":  kw["report_path"] = argv[i + 1]; i += 2
        elif a == "--tol":    kw["tol"] = float(argv[i + 1]); i += 2
        else: i += 1
    return kw


if __name__ == "__main__":
    args = _parse(sys.argv[1:])
    if not args["baseline_path"] or not args["report_path"]:
        print("usage: python -m backend.eval.gate --baseline <base.json> --check <new.json> [--tol 0.02]")
        sys.exit(2)
    sys.exit(0 if check(**args) else 1)

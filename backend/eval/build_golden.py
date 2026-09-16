"""Automated Golden Set v2 builder.

Samples real chunks across a project and drafts, per chunk, a natural question whose answer
lives ONLY in that chunk — keyed to that chunk_id (works on real BRDs that have no req_ids),
in the chunk's own language. Adds a few 'abstain' cases (off-topic questions). The result is
LLM-drafted and meant for a quick human review before it becomes the trusted yardstick.

Batched and throttled through the existing generation engine — no resource upgrade.

    python -m backend.eval.build_golden --project directives-brd --owner 6 --n 20
    python -m backend.eval.build_golden --project companion-calendar --owner 14 --n 15 --out data/golden/cc.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from ..db import pool
from ..generate import engine

_BATCH = 10
_MAXTEXT = 900
_ABSTAIN = [
    "What is the company's holiday leave policy?",
    "How do I reset my email password?",
    "ما هي سياسة استرداد الأموال للعملاء؟",
]


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _lang(text: str) -> str:
    return "ar" if re.search(r"[؀-ۿ]", text or "") else "en"


def _sample(owner_id: int, project: str, n: int) -> list[tuple[int, str]]:
    """(chunk_id, text) sampled evenly across the project's chunks in document order."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "select c.id, c.text from brd_chunk c join brd_document d on d.id = c.document_id "
            "where d.owner_id = %s and d.project = %s and d.status = 'ready' order by c.ordinal",
            (owner_id, project),
        )
        rows = cur.fetchall()
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


_PROMPT = """You are building a retrieval test set from a Business Requirements Document.
For EACH numbered requirement below, write ONE natural question a stakeholder would ask
whose answer is found ONLY in that requirement — specific enough that the right requirement
must be retrieved to answer it. Write the question in the SAME language as the requirement.
Do NOT quote any requirement id. No numbering, no quotes.

REQUIREMENTS:
{items}

Return ONLY a JSON array of {n} question strings, in the same order."""


def _questions(texts: list[str]) -> list[str]:
    items = "\n".join(f"{i + 1}. {_norm(t)[:_MAXTEXT]}" for i, t in enumerate(texts))
    out = engine.complete_chat(
        [{"role": "user", "content": _PROMPT.replace("{items}", items).replace("{n}", str(len(texts)))}],
        temperature=0.2, max_tokens=80 * len(texts) + 200,
    )
    m = re.search(r"\[.*\]", out or "", re.S)
    if not m:
        return [""] * len(texts)
    try:
        arr = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return [""] * len(texts)
    qs = [_norm(str(x)) for x in arr] if isinstance(arr, list) else []
    return (qs + [""] * len(texts))[:len(texts)]


def build(owner_id: int, project: str, n: int, out: str | None) -> None:
    sample = _sample(owner_id, project, n)
    if not sample:
        print(f"no ready chunks for owner {owner_id} / project {project!r}")
        return
    cases: list[dict] = []
    for i in range(0, len(sample), _BATCH):
        batch = sample[i:i + _BATCH]
        qs = _questions([t for _, t in batch])
        for (cid, text), q in zip(batch, qs):
            if q:
                cases.append({"question": q, "expect_chunk_ids": [cid], "lang": _lang(text), "type": "single"})
    for q in _ABSTAIN:
        cases.append({"question": q, "expect_chunk_ids": [], "lang": _lang(q), "type": "abstain"})

    out_path = Path(out) if out else Path("data") / "golden" / f"{project}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(
        {"description": f"Golden Set v2 for {project} (LLM-drafted, review before trusting).",
         "owner_id": owner_id, "project": project, "cases": cases},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(cases)} cases ({len(cases) - len(_ABSTAIN)} answerable + {len(_ABSTAIN)} abstain) → {out_path}")


def _parse(argv: list[str]) -> dict:
    kw: dict = {"owner_id": None, "project": None, "n": 20, "out": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--owner":     kw["owner_id"] = int(argv[i + 1]); i += 2
        elif a == "--project": kw["project"] = argv[i + 1]; i += 2
        elif a == "--n":       kw["n"] = int(argv[i + 1]); i += 2
        elif a == "--out":     kw["out"] = argv[i + 1]; i += 2
        else: i += 1
    return kw


if __name__ == "__main__":
    args = _parse(sys.argv[1:])
    if args["owner_id"] is None or not args["project"]:
        print("usage: python -m backend.eval.build_golden --project <name> --owner <id> [--n 20] [--out path]")
        sys.exit(2)
    build(**args)

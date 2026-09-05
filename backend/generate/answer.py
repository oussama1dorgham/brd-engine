"""Grounded, cited answering: retrieve -> prompt -> validate citations.

The model is told to answer ONLY from the numbered sources, cite them inline as
[n], and abstain with an exact sentence when the answer isn't present. We then
map the [n] it used back to the real chunks (req-ID / section / project), so
every answer is traceable — the portable stand-in for native citations.

    python -m backend.generate.answer "How do staff sign in to payments admin?" --project payments
"""
from __future__ import annotations

import re
import sys

from ..retrieve.retriever import retrieve
from .llm import chat, message_text

ABSTAIN = "The BRDs do not specify this."

SYSTEM = (
    "You answer questions about Business Requirements Documents (BRDs).\n"
    "Rules:\n"
    "- Use ONLY the numbered SOURCES below for facts. Never use outside knowledge or assumptions.\n"
    "- Any earlier conversation turns are context ONLY for interpreting what the question refers to "
    "(e.g. resolving 'this case' or 'these roles'); do not treat them as source facts. Scope the answer "
    "to what the question actually asks about, not the whole document.\n"
    "- Cite the source(s) for every claim inline with their bracket number, e.g. [1] or [2][3].\n"
    f'- If the sources do not contain the answer, reply with exactly: "{ABSTAIN}" and nothing else.\n'
    "- Write for a person: open with a short, natural sentence that directly answers, then add bullets or a "
    "compact table only when they genuinely aid clarity. Keep it concise and don't over-structure simple "
    "answers with many headings.\n"
    "- Be factual; quote specific values (numbers, standards) when the sources give them."
)

_CITE = re.compile(r"\[(\d+)\]")


def parse_citations(text: str, n_sources: int) -> list[int]:
    """Valid, de-duplicated source numbers (1..n_sources) cited in text, sorted."""
    return sorted({int(n) for n in _CITE.findall(text) if 1 <= int(n) <= n_sources})


def _sources_block(chunks: list[dict]) -> str:
    parts = []
    for i, c in enumerate(chunks, 1):
        tag = c["req_id"] or c["section"]
        parts.append(f"[{i}] ({tag} · project={c['project']})\n{c['text']}")
    return "\n\n".join(parts)


def answer(question: str, project: str | None = None, k: int = 5) -> dict:
    chunks = retrieve(question, k_final=k, project=project, use_rerank=True)
    if not chunks:
        return {"answer": ABSTAIN, "sources": [], "retrieved": [], "usage": None, "model": None}

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Question: {question}\n\nSOURCES:\n{_sources_block(chunks)}"},
    ]
    resp = chat(messages, temperature=0.0, max_tokens=600)
    text = message_text(resp).strip()
    if not text:
        text = "I couldn't produce an answer just now — the model returned an empty response. Please try again."

    # validate: keep only citations that point at a source we actually supplied
    cited_ns = parse_citations(text, len(chunks))
    sources = [{"n": n, **chunks[n - 1]} for n in cited_ns]

    return {"answer": text, "sources": sources, "retrieved": chunks,
            "usage": resp.usage, "model": resp.model}


def _parse_args(argv: list[str]) -> tuple[str, str | None]:
    project, words, i = None, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--project" and i + 1 < len(argv):
            project, i = argv[i + 1], i + 2
        elif a.startswith("--project="):
            project, i = a.split("=", 1)[1], i + 1
        else:
            words.append(a)
            i += 1
    return " ".join(words), project


if __name__ == "__main__":
    q, project = _parse_args(sys.argv[1:])
    res = answer(q, project=project)

    print(f"Q: {q}   (project={project or 'ALL'})\n")
    print("Answer:")
    print(f"  {res['answer']}\n")

    print("Sources cited:")
    if res["sources"]:
        for s in res["sources"]:
            print(f"  [{s['n']}] {s['req_id'] or s['section']}  (project={s['project']})")
    else:
        print("  (none — abstained, or answer had no citations)")

    retrieved = [c["req_id"] or c["section"] for c in res["retrieved"]]
    print(f"\nretrieved pool: {retrieved}")
    if res["usage"]:
        print(f"model={res['model']}  tokens={res['usage'].prompt_tokens} in / "
              f"{res['usage'].completion_tokens} out")

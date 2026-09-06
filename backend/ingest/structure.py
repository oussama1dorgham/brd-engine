"""Reconstruct row structure from a flattened table blob — without content drift.

A table that was ingested as one line (no row delimiters — see docx_to_md's
prose fallback and the chunker's word-window) can't be edited safely: a human
editing the pipe-joined blob easily merges or misaligns rows, so a requirement
change becomes inaccurate.

The safe fix is NOT to have the model rewrite the text (it would paraphrase or
drop content on a long Arabic blob). Instead the model returns ONLY the ordered
leading phrase of each row (an "anchor"); we then locate each anchor in the
ORIGINAL text and slice between anchors. Every produced row is therefore a
literal substring of the source — concatenating the rows reproduces the input,
so the model can choose boundaries but can never invent or lose content.
"""
from __future__ import annotations

import json
import re

from ..generate import llm

_PROMPT = (
    "The text below is a table that was flattened onto a single line. A '|' "
    "separates a row's first cell (e.g. a name or label) from the rest of that "
    "row. Identify where each ROW begins.\n\n"
    "Return ONLY a compact JSON array of strings: the exact leading substring "
    "(the first 2-6 words, copied VERBATIM from the text) that starts each row, "
    "in order of appearance. Copy the substrings exactly as written — do not "
    "translate, reword, summarize, or add anything. Output only the JSON array, "
    "nothing else.\n\nTEXT:\n"
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _parse_json_array(raw: str) -> list[str]:
    """Pull the JSON array out of the model reply, tolerating code fences / prose."""
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    return [str(x) for x in data if isinstance(x, str) and str(x).strip()]


def _find_anchors(text: str, anchors: list[str]) -> list[int]:
    """Start offset of each anchor in `text`, searched in order (whitespace-tolerant).
    Anchors that don't match in order are skipped, so a stray hallucinated anchor
    can't create a spurious boundary."""
    positions: list[int] = []
    cursor = 0
    for a in anchors:
        words = _norm(a).split(" ")
        if not words or not words[0]:
            continue
        pattern = re.compile(r"\s*".join(re.escape(w) for w in words))
        m = pattern.search(text, cursor)
        if not m:
            continue
        positions.append(m.start())
        cursor = m.end()
    return positions


def looks_flattened(text: str) -> bool:
    """A single-line blob carrying more than one '|' — i.e. several table rows
    fused together. Multi-line or single-pipe text is already editable as-is."""
    return "\n" not in text.strip() and text.count("|") >= 2


def propose_rows(text: str, model: str | None = None) -> dict:
    """Slice `text` into rows at model-identified boundaries.

    Returns {"rows": [...], "conserved": bool}. `rows` are literal substrings of
    `text`; `conserved` is True when they perfectly reconstruct the input
    (non-space characters preserved exactly). On any failure it returns the input
    unchanged as a single row, conserved True (a no-op the caller can ignore).
    """
    text = text.strip()
    if not text:
        return {"rows": [], "conserved": True}

    try:
        resp = llm.chat(
            [{"role": "user", "content": _PROMPT + text}],
            temperature=0.0, max_tokens=500, model=model,
        )
        anchors = _parse_json_array(llm.message_text(resp))
    except Exception:
        return {"rows": [text], "conserved": True}

    positions = _find_anchors(text, anchors)
    if len(positions) < 2:
        return {"rows": [text], "conserved": True}

    positions[0] = 0                      # keep any preamble with the first row
    rows: list[str] = []
    for i, start in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        piece = text[start:end].strip()
        if piece:
            rows.append(piece)

    conserved = _conserves(text, rows)
    if not conserved or len(rows) < 2:
        return {"rows": [text], "conserved": True}
    return {"rows": rows, "conserved": True}


def _conserves(original: str, rows: list[str]) -> bool:
    """True when the rows reproduce the original exactly, ignoring only whitespace
    (the boundaries we added/trimmed). Guarantees no content was invented or lost."""
    strip_ws = lambda s: re.sub(r"\s+", "", s)
    return strip_ws("".join(rows)) == strip_ws(original)


if __name__ == "__main__":
    import sys
    sample = sys.argv[1] if len(sys.argv) > 1 else (
        "Admin | creates settings, weekly reports. Manager | manages portfolio. "
        "Officer | coordinates and delegates."
    )
    out = propose_rows(sample)
    print("conserved:", out["conserved"], "rows:", len(out["rows"]))
    for r in out["rows"]:
        print(" -", r)

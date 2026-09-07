"""Story-driven requirement changes — the QA user describes a change in plain
words and the system proposes concrete, PRECISE edits, grounded in the BRD.

Design goal: TRUST through deterministic, total coverage. The set of affected
requirements is the retrieval result (a pure function of the corpus + query, and
cached), so it is identical every run. Every requirement in that set is then given
an explicit decision — an edit, or an explicit "no change" with a reason — so
nothing is silently dropped and the scopes the user sees don't wobble between
prompts. Only the wording of an edit can vary (inherent to any LLM); which scopes
are surfaced does not.

Flow: refine the story (shown back) → retrieve the affected scope (deterministic)
→ detect any genuinely-new requirements to ADD → force an edit/none decision for
EVERY affected requirement → return the plan for review → apply approved ops
through the normal safe pipeline (snapshot → re-embed → re-index → cache-bust →
draft → approve).

Precision: an edit is a minimal find/replace — the smallest verbatim span to
change plus its replacement, located in the ORIGINAL text and swapped in place, so
everything the change doesn't touch stays byte-identical.
"""
from __future__ import annotations

import json
import re

from ..ingest.edit import add_requirement, remove_requirement, update_requirement
from ..retrieve.retriever import retrieve
from . import llm

# how many retrieved requirements form the (deterministic) affected scope
SCOPE_K = 8
SCOPE_CANDIDATES = 16

_REFINE_PROMPT = """Rewrite the user's requirement-change request as ONE precise, unambiguous
instruction for editing a Business Requirements Document. Keep it strictly faithful — do not
invent scope, add requirements they didn't ask for, or drop any part of their intent. Preserve
their language. One or two sentences. Output ONLY the rewritten instruction, no preamble.

USER REQUEST:
{story}
"""

_ADD_PROMPT = """A change is being made to a Business Requirements Document:

CHANGE: {change}

Below are the existing requirements most related to it. If — and ONLY if — the change requires
adding one or more genuinely NEW requirements that are not already present, return them; place
each after the most relevant existing chunk_id. Keep the document's language and table shape.
If nothing new needs adding, return an empty list.

Return ONLY JSON (no prose, no code fences):
{"operations": [ {"op": "add", "after_chunk_id": 123, "req_id": "FR-9", "new_text": "...", "reason": "..."} ]}

EXISTING REQUIREMENTS:
{candidates}
"""

_COVERAGE_PROMPT = """A change is being made to a Business Requirements Document:

CHANGE: {change}

Below is a list of existing requirements that may be affected. For EVERY requirement listed you
MUST return exactly one decision — never omit or invent a chunk_id. Decide whether the
requirement must be edited to reflect or stay consistent with the change:
- needs an edit → {"chunk_id": N, "action": "edit", "find": "<exact verbatim span to change>",
  "replace": "<new span>", "reason": "<why>"}. "find" is the smallest span copied VERBATIM from
  that requirement; keep the original language and any "a | b" table shape.
- needs no change → {"chunk_id": N, "action": "none", "reason": "<why it stays as-is>"}.

Return ONLY JSON (no prose, no code fences):
{"decisions": [ ... one per chunk_id, in the same order ... ]}

REQUIREMENTS:
{requirements}
"""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _locate(text: str, find: str) -> tuple[int, int] | None:
    """Find `find` in `text`: exact first, then whitespace-tolerant. Returns the
    (start, end) span in the ORIGINAL text, or None if it isn't there."""
    if not find:
        return None
    i = text.find(find)
    if i >= 0:
        return (i, i + len(find))
    words = _norm(find).split(" ")
    if not words or not words[0]:
        return None
    m = re.compile(r"\s*".join(re.escape(w) for w in words)).search(text)
    return (m.start(), m.end()) if m else None


def _parse_json(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def refine_story(story: str, model: str | None = None) -> str:
    """Restate the user's request as one precise instruction (best-effort)."""
    story = (story or "").strip()
    if not story:
        return ""
    try:
        resp = llm.chat([{"role": "user", "content": _REFINE_PROMPT.replace("{story}", story)}],
                        temperature=0.0, max_tokens=200, model=model)
        return _norm(llm.message_text(resp)) or story
    except Exception:
        return story


def _candidate_lines(hits: list[dict]) -> str:
    out = []
    for h in hits:
        label = h["req_id"] or h["section"] or f"#{h['chunk_id']}"
        out.append(f'- chunk_id {h["chunk_id"]} [{label}]: {_norm(h["text"] or "")[:450]}')
    return "\n".join(out)


def _detect_adds(change: str, hits: list[dict], by_id: dict, model: str | None) -> list[dict]:
    """New requirements to ADD (0+). Deterministic set is irrelevant here — an add is
    net-new — but placement references an existing chunk_id."""
    prompt = _ADD_PROMPT.replace("{change}", change).replace("{candidates}", _candidate_lines(hits))
    try:
        resp = llm.chat([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=700, model=model)
        raw = _parse_json(llm.message_text(resp)).get("operations")
    except Exception:
        return []
    out = []
    for op in (raw if isinstance(raw, list) else []):
        if not isinstance(op, dict) or op.get("op") != "add":
            continue
        new_text = (op.get("new_text") or "").strip()
        if not new_text:
            continue
        after = op.get("after_chunk_id")
        after = after if after in by_id else None
        out.append({"op": "add", "scope": "primary", "new_text": new_text, "after_chunk_id": after,
                    "req_id": (op.get("req_id") or None),
                    "label": after and (by_id[after]["req_id"] or f"#{after}"),
                    "reason": str(op.get("reason") or "")})
    return out


def _coverage(change: str, hits: list[dict], model: str | None) -> dict[int, dict]:
    """Force an edit/none decision for EVERY affected requirement. Returns
    {chunk_id: {"action","find","replace","reason"}}. Missing/failed ids default to
    'none' so a scope is never silently dropped."""
    prompt = _COVERAGE_PROMPT.replace("{change}", change).replace("{requirements}", _candidate_lines(hits))
    decided: dict[int, dict] = {}
    try:
        resp = llm.chat([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=1200, model=model)
        raw = _parse_json(llm.message_text(resp)).get("decisions")
    except Exception:
        raw = None
    for d in (raw if isinstance(raw, list) else []):
        if isinstance(d, dict) and d.get("chunk_id") is not None:
            decided[d["chunk_id"]] = d
    return decided


def plan_change(owner_id: int, project: str, story: str, model: str | None = None,
                refine: bool = True) -> dict:
    """Propose precise operations for a plain-language change (no persistence).

    Returns {refined, operations, related, candidates}. The affected set (retrieval)
    is deterministic; every requirement in it is either edited (an op) or listed in
    `related` with a reason it needs no change — so coverage is total and stable."""
    story = (story or "").strip()
    if not story:
        return {"refined": "", "operations": [], "related": [], "candidates": []}

    # Retrieve on the user's ORIGINAL words (a fixed input) so the affected set is
    # deterministic across runs — refining the query would make retrieval, and thus
    # the scopes shown, wobble between identical prompts. Refinement is for the user's
    # display and the edit-planning prompts only.
    refined = refine_story(story, model) if refine else story
    hits = retrieve(story, owner_id=owner_id, project=project, k_final=SCOPE_K, candidates=SCOPE_CANDIDATES)
    by_id = {h["chunk_id"]: h for h in hits}
    if not hits:
        return {"refined": refined, "operations": [], "related": [], "candidates": [],
                "error": "No requirements found for this BRD yet."}

    adds = _detect_adds(refined, hits, by_id, model)
    decisions = _coverage(refined, hits, model)

    edit_ops: list[dict] = []
    related_out: list[dict] = []
    for h in hits:                                   # retrieval order = stable priority
        cid = h["chunk_id"]
        old = h["text"] or ""
        d = decisions.get(cid)
        label = h["req_id"] or h["section"] or f"#{cid}"
        if d and d.get("action") == "edit":
            span = _locate(old, d.get("find") or "")
            replace = d.get("replace") or ""
            if span is not None and replace:
                s, e = span
                new = old[:s] + replace + old[e:]
                if new.strip() != old.strip():
                    edit_ops.append({"op": "edit", "chunk_id": cid, "changes": [{"find": old[s:e], "replace": replace}],
                                     "old_text": old, "new_text": new, "label": label,
                                     "reason": str(d.get("reason") or "")})
                    continue
            # edit requested but not applyable precisely → surface as awareness, not dropped
            related_out.append({"chunk_id": cid, "label": label, "text": old,
                                "reason": str(d.get("reason") or "Flagged as affected — review manually.")})
        else:
            reason = str((d or {}).get("reason") or "No change needed to stay consistent.")
            related_out.append({"chunk_id": cid, "label": label, "text": old, "reason": reason})

    # Scope: an add makes the add primary and edits consistency; an edit-only change
    # marks the top-ranked edited requirement primary and the rest consistency.
    if adds:
        for e in edit_ops:
            e["scope"] = "consistency"
    else:
        for idx, e in enumerate(edit_ops):
            e["scope"] = "primary" if idx == 0 else "consistency"

    operations = adds + edit_ops
    return {"refined": refined, "operations": operations, "related": related_out,
            "candidates": [{"chunk_id": h["chunk_id"], "label": h["req_id"] or h["section"] or f"#{h['chunk_id']}",
                            "text": h["text"]} for h in hits]}


def apply_operations(owner_id: int, project: str, operations: list[dict],
                     changed_by: int | None = None) -> dict:
    """Apply reviewed operations in order via the normal edit pipeline.

    Ownership is enforced by the underlying functions (a foreign chunk_id raises
    ChunkNotFound). Returns {applied, edited, added, deleted, results}."""
    edited = added = deleted = 0
    results: list[dict] = []
    for op in operations or []:
        kind = op.get("op")
        try:
            if kind == "edit":
                update_requirement(owner_id, int(op["chunk_id"]), (op.get("new_text") or "").strip(),
                                   changed_by=changed_by, kind="edit")
                edited += 1
                results.append({"op": "edit", "chunk_id": op["chunk_id"], "ok": True})
            elif kind == "add":
                r = add_requirement(owner_id, project, (op.get("new_text") or "").strip(),
                                    req_id=(op.get("req_id") or None),
                                    after_chunk_id=op.get("after_chunk_id"), changed_by=changed_by)
                added += 1
                results.append({"op": "add", "chunk_id": r.get("chunk_id"), "ok": True})
            elif kind == "delete":
                remove_requirement(owner_id, int(op["chunk_id"]), changed_by=changed_by)
                deleted += 1
                results.append({"op": "delete", "chunk_id": op["chunk_id"], "ok": True})
        except Exception as e:  # noqa: BLE001
            results.append({"op": kind, "chunk_id": op.get("chunk_id"), "ok": False,
                            "error": f"{type(e).__name__}: {e}"})
    return {"applied": edited + added + deleted, "edited": edited, "added": added,
            "deleted": deleted, "results": results}

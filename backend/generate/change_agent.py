"""Story-driven requirement changes — the QA user describes a change in plain
words and the system proposes concrete, PRECISE edits, grounded in the BRD.

Flow: refine the user's story into a clear instruction (shown back to the user)
→ retrieve the requirements most relevant to it → ask the LLM to plan a small set
of operations (edit / add / delete) → return the plan for the user to review and
tweak → apply the approved operations through the normal safe pipeline (snapshot
→ re-embed → re-index → cache-bust → draft → approve).

Precision: an edit is expressed as a minimal find/replace — the smallest verbatim
span of the requirement to change, plus its replacement. The server locates that
span in the ORIGINAL text and swaps only it, so everything the change doesn't
touch stays byte-identical (no whole-chunk rewrite, no dropped sibling content).

Grounding & safety:
  * the planner may only edit/delete chunk ids that exist in the BRD (validated
    server-side; the edit functions re-check ownership),
  * an edit whose `find` can't be located verbatim is dropped (never applied loosely),
  * planning never writes — it only proposes,
  * applying reuses update/add/remove_requirement, so every invariant holds.
"""
from __future__ import annotations

import json
import re

from ..ingest.edit import add_requirement, remove_requirement, update_requirement
from ..retrieve.retriever import retrieve
from . import llm

_REFINE_PROMPT = """Rewrite the user's requirement-change request as ONE precise, unambiguous
instruction for editing a Business Requirements Document. Keep it strictly faithful — do not
invent scope, add requirements they didn't ask for, or drop any part of their intent. Preserve
their language. One or two sentences. Output ONLY the rewritten instruction, no preamble.

USER REQUEST:
{story}
"""

_PLAN_PROMPT = """You are a requirements analyst maintaining a Business Requirements Document (BRD).
Using ONLY the candidate requirements below, plan the smallest set of PRECISE operations that
realizes the change.

Rules:
- Prefer editing an existing requirement over adding a new one when the change fits one.
- An EDIT must be a minimal find/replace: "find" is the exact, smallest span copied VERBATIM
  from that requirement's text (enough to be unique within it), and "replace" is what it becomes.
  Change only the part that must change — never restate the whole requirement. Keep the original
  language (e.g. Arabic) and any "a | b" table shape.
- Only use chunk_id values that appear in the candidates for "edit" and "delete".
- Use "add" for genuinely new requirements; optionally set after_chunk_id (a candidate id) and
  req_id if the user named one.
- Do nothing speculative.

Consider the SCOPE the change touches. Every operation has a "scope":
- "primary" — the operation that directly realizes the user's request.
- "consistency" — a knock-on change to ANOTHER requirement that must ALSO change to stay
  consistent with the primary change (e.g. the primary adds an SMS reminder, so the existing
  "notification channels" requirement must be edited to include SMS). Use the same precise
  edit/add/delete shape; these will be shown to the user to approve or skip.
Only put a requirement in "related" (awareness only, no edit) when it is affected/worth
reviewing but genuinely needs NO text change. A given chunk_id appears in at most one place.

Return ONLY JSON of this exact shape (no prose, no code fences):
{"operations": [
   {"op": "edit", "scope": "primary", "chunk_id": 123, "find": "<verbatim span>", "replace": "<new span>", "reason": "..."},
   {"op": "edit", "scope": "consistency", "chunk_id": 456, "find": "<verbatim span>", "replace": "<new span>", "reason": "..."},
   {"op": "add", "scope": "primary", "after_chunk_id": 123, "req_id": "FR-9", "new_text": "...", "reason": "..."},
   {"op": "delete", "scope": "consistency", "chunk_id": 789, "reason": "..."}
 ],
 "related": [ {"chunk_id": 123, "reason": "<how the change touches this requirement, needing no edit>"} ]}

CANDIDATE REQUIREMENTS:
{candidates}

CHANGE TO MAKE:
{story}
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
    """Restate the user's request as one precise instruction (best-effort; returns
    the original on any failure)."""
    story = (story or "").strip()
    if not story:
        return ""
    try:
        resp = llm.chat([{"role": "user", "content": _REFINE_PROMPT.replace("{story}", story)}],
                        temperature=0.0, max_tokens=200, model=model)
        out = _norm(llm.message_text(resp))
        return out or story
    except Exception:
        return story


def plan_change(owner_id: int, project: str, story: str, model: str | None = None,
                refine: bool = True) -> dict:
    """Propose precise operations for a plain-language change (no persistence).

    Returns {refined, operations, candidates}. When `refine` is True the story is
    first rewritten into a clear instruction (returned as `refined`) and used for
    retrieval + planning; pass refine=False to plan the given text verbatim (e.g.
    after the user edits the refined instruction)."""
    story = (story or "").strip()
    if not story:
        return {"refined": "", "operations": [], "candidates": []}

    refined = refine_story(story, model) if refine else story
    hits = retrieve(refined, owner_id=owner_id, project=project, k_final=8, candidates=16)
    by_id = {h["chunk_id"]: h for h in hits}
    if not hits:
        return {"refined": refined, "operations": [], "candidates": [],
                "error": "No requirements found for this BRD yet."}

    cand_lines = []
    for h in hits:
        label = h["req_id"] or h["section"] or f"#{h['chunk_id']}"
        cand_lines.append(f'- chunk_id {h["chunk_id"]} [{label}]: {_norm(h["text"] or "")[:500]}')
    prompt = (_PLAN_PROMPT
              .replace("{candidates}", "\n".join(cand_lines))
              .replace("{story}", refined))

    try:
        resp = llm.chat([{"role": "user", "content": prompt}],
                        temperature=0.0, max_tokens=900, model=model)
        plan = _parse_json(llm.message_text(resp))
    except Exception as e:  # noqa: BLE001
        return {"refined": refined, "operations": [], "candidates": [],
                "error": f"Couldn't plan the change: {llm.describe_error(e)}"}

    raw_ops = plan.get("operations")
    raw_ops = raw_ops if isinstance(raw_ops, list) else []

    # Fold multiple edits on the same chunk into one precise op (apply each
    # find/replace in turn to the original text) so they don't overwrite each other.
    edits: dict[int, dict] = {}
    others: list[dict] = []
    for op in raw_ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "edit":
            cid = op.get("chunk_id")
            if cid not in by_id:
                continue
            find, replace = op.get("find") or "", op.get("replace") or ""
            entry = edits.setdefault(cid, {"changes": [], "reasons": [], "scope": "primary"})
            entry["changes"].append({"find": find, "replace": replace})
            if op.get("scope") == "consistency":
                entry["scope"] = "consistency"
            if op.get("reason"):
                entry["reasons"].append(str(op["reason"]))
        elif kind in ("add", "delete"):
            others.append(op)

    ops_out: list[dict] = []
    for cid, entry in edits.items():
        old_text = by_id[cid]["text"] or ""
        new_text = old_text
        applied = []
        for ch in entry["changes"]:
            span = _locate(new_text, ch["find"])
            if span is None or not ch["replace"]:
                continue                             # can't place it precisely — drop
            s, e = span
            applied.append({"find": new_text[s:e], "replace": ch["replace"]})
            new_text = new_text[:s] + ch["replace"] + new_text[e:]
        if not applied or new_text.strip() == old_text.strip():
            continue
        ops_out.append({"op": "edit", "scope": entry["scope"], "chunk_id": cid, "changes": applied,
                        "old_text": old_text, "new_text": new_text,
                        "label": by_id[cid]["req_id"] or by_id[cid]["section"] or f"#{cid}",
                        "reason": " ".join(entry["reasons"])})

    for op in others:
        scope = "consistency" if op.get("scope") == "consistency" else "primary"
        if op["op"] == "delete":
            cid = op.get("chunk_id")
            if cid not in by_id:
                continue
            ops_out.append({"op": "delete", "scope": scope, "chunk_id": cid, "old_text": by_id[cid]["text"],
                            "label": by_id[cid]["req_id"] or by_id[cid]["section"] or f"#{cid}",
                            "reason": str(op.get("reason") or "")})
        else:  # add
            new_text = (op.get("new_text") or "").strip()
            if not new_text:
                continue
            after = op.get("after_chunk_id")
            after = after if after in by_id else None
            ops_out.append({"op": "add", "scope": scope, "new_text": new_text, "after_chunk_id": after,
                            "req_id": (op.get("req_id") or None),
                            "label": after and (by_id[after]["req_id"] or f"#{after}"),
                            "reason": str(op.get("reason") or "")})

    # Scope awareness: requirements the change touches/depends on but doesn't directly
    # edit — surfaced so the user sees the ripple. Exclude ones already changed.
    changed_ids = {o["chunk_id"] for o in ops_out if o["op"] in ("edit", "delete")}
    related_out: list[dict] = []
    seen: set[int] = set()
    for rel in (plan.get("related") if isinstance(plan.get("related"), list) else []):
        if not isinstance(rel, dict):
            continue
        cid = rel.get("chunk_id")
        if cid not in by_id or cid in changed_ids or cid in seen:
            continue
        seen.add(cid)
        related_out.append({"chunk_id": cid,
                            "label": by_id[cid]["req_id"] or by_id[cid]["section"] or f"#{cid}",
                            "text": by_id[cid]["text"], "reason": str(rel.get("reason") or "")})

    return {"refined": refined, "operations": ops_out, "related": related_out,
            "candidates": [{"chunk_id": h["chunk_id"],
                            "label": h["req_id"] or h["section"] or f"#{h['chunk_id']}",
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

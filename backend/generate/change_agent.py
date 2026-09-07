"""Story-driven requirement changes — the QA user describes a change in plain
words and the system proposes concrete edits/additions, grounded in the BRD.

Flow: retrieve the requirements most relevant to the story → ask the LLM to plan
a small set of operations (edit / add / delete) referencing real chunk ids →
return the plan for the user to review and tweak → apply the approved operations
through the normal safe pipeline (snapshot → re-embed → re-index → cache-bust →
draft → approve).

Grounding & safety:
  * the planner may only edit/delete chunk ids that exist in the BRD (validated
    server-side; the edit functions re-check ownership, so a bad id is rejected),
  * planning never writes — it only proposes,
  * applying reuses update/add/remove_requirement, so every invariant (atomic
    text+embedding, draft snapshot, cache bust, audit) still holds.
"""
from __future__ import annotations

import json
import re

from ..ingest.edit import add_requirement, remove_requirement, update_requirement
from ..retrieve.retriever import retrieve
from . import llm

_PLAN_PROMPT = """You are a requirements analyst maintaining a Business Requirements Document (BRD).
The user describes a change in plain language. Using ONLY the candidate requirements below,
plan the smallest set of concrete operations that realizes the change.

Rules:
- Prefer EDITING an existing requirement over adding a new one when the change fits one.
- For an edit, return the FULL revised text of that requirement, preserving everything the
  change doesn't touch. Keep the original language (e.g. Arabic) and any "a | b" table-row
  shape intact.
- Only use chunk_id values that appear in the candidates for "edit" and "delete".
- Use "add" for genuinely new requirements; optionally set after_chunk_id (a candidate id)
  to place it, and req_id if the user names one.
- Do nothing speculative. If the change clearly targets nothing here, return an "add".

Return ONLY JSON of this exact shape (no prose, no code fences):
{"summary": "<one sentence describing the change>",
 "operations": [
   {"op": "edit", "chunk_id": 123, "new_text": "...", "reason": "..."},
   {"op": "add", "after_chunk_id": 123, "req_id": "FR-9", "new_text": "...", "reason": "..."},
   {"op": "delete", "chunk_id": 123, "reason": "..."}
 ]}

CANDIDATE REQUIREMENTS:
{candidates}

USER'S CHANGE:
{story}
"""


def _parse_plan(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"summary": "", "operations": []}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {"summary": "", "operations": []}
    if not isinstance(data, dict):
        return {"summary": "", "operations": []}
    ops = data.get("operations")
    return {"summary": str(data.get("summary") or ""),
            "operations": ops if isinstance(ops, list) else []}


def plan_change(owner_id: int, project: str, story: str, model: str | None = None) -> dict:
    """Propose operations for a plain-language change (no persistence).

    Returns {summary, operations, candidates}. Each operation is validated and
    enriched with the target's current text/label so the UI can show a diff."""
    story = (story or "").strip()
    if not story:
        return {"summary": "", "operations": [], "candidates": []}

    hits = retrieve(story, owner_id=owner_id, project=project, k_final=8, candidates=16)
    by_id = {h["chunk_id"]: h for h in hits}
    if not hits:
        return {"summary": "", "operations": [], "candidates": [],
                "error": "No requirements found for this BRD yet."}

    cand_lines = []
    for h in hits:
        label = h["req_id"] or h["section"] or f"#{h['chunk_id']}"
        text = " ".join((h["text"] or "").split())
        cand_lines.append(f'- chunk_id {h["chunk_id"]} [{label}]: {text[:400]}')
    prompt = (_PLAN_PROMPT
              .replace("{candidates}", "\n".join(cand_lines))
              .replace("{story}", story))

    try:
        resp = llm.chat([{"role": "user", "content": prompt}],
                        temperature=0.0, max_tokens=900, model=model)
        plan = _parse_plan(llm.message_text(resp))
    except Exception as e:  # noqa: BLE001
        return {"summary": "", "operations": [], "candidates": [],
                "error": f"Couldn't plan the change: {llm.describe_error(e)}"}

    ops_out = []
    for op in plan["operations"]:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "edit":
            cid = op.get("chunk_id")
            if cid not in by_id:
                continue
            new_text = (op.get("new_text") or "").strip()
            if not new_text or new_text == (by_id[cid]["text"] or "").strip():
                continue
            ops_out.append({"op": "edit", "chunk_id": cid, "new_text": new_text,
                            "old_text": by_id[cid]["text"],
                            "label": by_id[cid]["req_id"] or by_id[cid]["section"] or f"#{cid}",
                            "reason": str(op.get("reason") or "")})
        elif kind == "delete":
            cid = op.get("chunk_id")
            if cid not in by_id:
                continue
            ops_out.append({"op": "delete", "chunk_id": cid, "old_text": by_id[cid]["text"],
                            "label": by_id[cid]["req_id"] or by_id[cid]["section"] or f"#{cid}",
                            "reason": str(op.get("reason") or "")})
        elif kind == "add":
            new_text = (op.get("new_text") or "").strip()
            if not new_text:
                continue
            after = op.get("after_chunk_id")
            after = after if after in by_id else None
            ops_out.append({"op": "add", "new_text": new_text,
                            "after_chunk_id": after, "req_id": (op.get("req_id") or None),
                            "label": after and (by_id[after]["req_id"] or f"#{after}"),
                            "reason": str(op.get("reason") or "")})

    return {"summary": plan["summary"], "operations": ops_out,
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

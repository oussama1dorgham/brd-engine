"""Story-driven requirement changes — the QA user describes a change in plain
words and the system proposes concrete, PRECISE edits, grounded in the BRD.

Design goals:
  * TRUST through a deterministic, total affected set. The set is the retrieval
    result on the user's ORIGINAL words (a pure function of corpus + query, cached),
    so it is identical every run and every requirement in it is always shown.
  * The agent AUTO-proposes only the PRIMARY change (what the story directly asks
    for). Every other affected requirement is listed as "related" with a reason —
    and is edited ON DEMAND: the user picks one and the agent generates a precise
    edit for just that requirement (propose_scope_edit), or the user edits it by
    hand. So which touched scopes change is user-driven, not left to the model.

Precision: an edit is a minimal find/replace — the smallest verbatim span to change
plus its replacement, located in the ORIGINAL text and swapped in place, so
everything the change doesn't touch stays byte-identical.
"""
from __future__ import annotations

import json
import re
from typing import NamedTuple

from ..db import pool
from ..ingest.edit import add_requirement, remove_requirement, update_requirement
from ..providers.base import ProviderError
from ..retrieve.retriever import retrieve
from . import engine

SCOPE_K = 8            # size of the (deterministic) affected set
SCOPE_CANDIDATES = 16


class GenRoute(NamedTuple):
    """The generation route for an edit call: a user's BYOK provider + chosen
    model, or all-None to fall back to the system OpenRouter + GEN_MODEL. Mirrors
    how the Q&A path (ChatSession) carries gen_provider/gen_key/gen_base_url/gen_model."""
    provider: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


def _gen(messages: list[dict], route: "GenRoute | None", *, max_tokens: int) -> str:
    """One-shot completion through the provider-agnostic engine. An empty route
    routes to the system fallback; engine.complete_chat returns plain text."""
    r = route or GenRoute()
    return engine.complete_chat(messages, provider=r.provider, api_key=r.api_key,
                                base_url=r.base_url, model=r.model,
                                temperature=0.0, max_tokens=max_tokens)

_REFINE_PROMPT = """Rewrite the user's requirement-change request as ONE precise, unambiguous
instruction for editing a Business Requirements Document. Keep it strictly faithful — do not
invent scope, add requirements they didn't ask for, or drop any part of their intent. Preserve
their language. One or two sentences. Output ONLY the rewritten instruction, no preamble.

USER REQUEST:
{story}
"""

_PLAN_PROMPT = """You are a requirements analyst maintaining a Business Requirements Document.
Using ONLY the candidate requirements below, realize the user's change.

Return TWO things:
- "operations": the DIRECT change the user asked for, and only that. Prefer editing the one
  requirement the request targets; add a new requirement only if the request is genuinely new.
  Do NOT edit requirements that are merely related — those go in "related".
    * edit  = minimal find/replace: "find" is the smallest span copied VERBATIM from that
      requirement, "replace" is what it becomes. Keep the original language and any "a | b" shape.
    * add   = a new requirement; optionally after_chunk_id (a candidate) and req_id.
    * delete= remove a requirement.
- "related": EVERY other candidate that the change touches or should be reviewed for consistency,
  each {chunk_id, reason}. These are offered to the user to edit on demand — do not edit them here.

Only use candidate chunk_ids. Return ONLY JSON (no prose, no code fences):
{"operations": [
   {"op": "edit", "chunk_id": 123, "find": "<verbatim>", "replace": "<new>", "reason": "..."},
   {"op": "add", "after_chunk_id": 123, "req_id": "FR-9", "new_text": "...", "reason": "..."}
 ],
 "related": [ {"chunk_id": 456, "reason": "<how the change touches this>"} ]}

CANDIDATE REQUIREMENTS:
{candidates}

CHANGE TO MAKE:
{change}
"""

_SCOPE_EDIT_PROMPT = """A change is being made to a Business Requirements Document:

CHANGE: {change}

Here is ONE existing requirement to align with that change:
{requirement}

Propose the precise minimal find/replace edit to make it consistent — "find" is the smallest
span copied VERBATIM from the requirement, "replace" is what it becomes. Keep the original
language and any "a | b" table shape. If it genuinely needs no change, say so.

Return ONLY JSON (no prose, no code fences):
{"action": "edit", "find": "<verbatim span>", "replace": "<new span>", "reason": "..."}
  or  {"action": "none", "reason": "<why it stays as-is>"}
"""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _locate(text: str, find: str) -> tuple[int, int] | None:
    """Find `find` in `text`: exact first, then whitespace-tolerant."""
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


def _err(e: Exception) -> str:
    """User-facing message for a failed generation. engine wraps provider failures
    as ProviderError with an already-friendly message; anything else falls back to str."""
    return str(e) if isinstance(e, ProviderError) else f"{type(e).__name__}: {e}"


def _parse_json(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _edit_op(cid: int, old: str, find: str, replace: str, reason: str, label: str,
             scope: str) -> dict | None:
    """Build a precise edit op by locating `find` in `old` and swapping in `replace`."""
    span = _locate(old, find or "")
    if span is None or not replace:
        return None
    s, e = span
    new = old[:s] + replace + old[e:]
    if new.strip() == old.strip():
        return None
    return {"op": "edit", "scope": scope, "chunk_id": cid, "changes": [{"find": old[s:e], "replace": replace}],
            "old_text": old, "new_text": new, "label": label, "reason": str(reason or "")}


def refine_story(story: str, route: "GenRoute | None" = None) -> str:
    story = (story or "").strip()
    if not story:
        return ""
    try:
        text = _gen([{"role": "user", "content": _REFINE_PROMPT.replace("{story}", story)}],
                    route, max_tokens=200)
        return _norm(text) or story
    except Exception:
        return story


def _candidate_lines(hits: list[dict]) -> str:
    out = []
    for h in hits:
        label = h["req_id"] or h["section"] or f"#{h['chunk_id']}"
        out.append(f'- chunk_id {h["chunk_id"]} [{label}]: {_norm(h["text"] or "")[:450]}')
    return "\n".join(out)


def plan_change(owner_id: int, project: str, story: str, route: "GenRoute | None" = None,
                refine: bool = True) -> dict:
    """Propose the PRIMARY change + list the related scopes (no persistence).

    The affected set (retrieval on the original story) is deterministic. Operations
    hold only the direct change; every other affected requirement is in `related`
    with a reason, to be edited on demand. Returns {refined, operations, related}."""
    story = (story or "").strip()
    if not story:
        return {"refined": "", "operations": [], "related": []}

    # Retrieve on the ORIGINAL words so the affected set is stable across runs
    # (refining the query would make retrieval, and the scopes shown, wobble).
    refined = refine_story(story, route) if refine else story
    hits = retrieve(story, owner_id=owner_id, project=project, k_final=SCOPE_K, candidates=SCOPE_CANDIDATES)
    by_id = {h["chunk_id"]: h for h in hits}
    if not hits:
        return {"refined": refined, "operations": [], "related": [],
                "error": "No requirements found for this BRD yet."}

    prompt = (_PLAN_PROMPT.replace("{candidates}", _candidate_lines(hits)).replace("{change}", refined))
    try:
        plan = _parse_json(_gen([{"role": "user", "content": prompt}], route, max_tokens=900))
    except Exception as e:  # noqa: BLE001
        return {"refined": refined, "operations": [], "related": [],
                "error": f"Couldn't plan the change: {_err(e)}"}

    raw_ops = plan.get("operations") if isinstance(plan.get("operations"), list) else []
    ops_out: list[dict] = []
    targeted: set[int] = set()
    for op in raw_ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        label = lambda cid: by_id[cid]["req_id"] or by_id[cid]["section"] or f"#{cid}"
        if kind == "edit" and op.get("chunk_id") in by_id:
            cid = op["chunk_id"]
            eo = _edit_op(cid, by_id[cid]["text"] or "", op.get("find") or "", op.get("replace") or "",
                          op.get("reason") or "", label(cid), "primary")
            if eo:
                ops_out.append(eo); targeted.add(cid)
        elif kind == "delete" and op.get("chunk_id") in by_id:
            cid = op["chunk_id"]
            ops_out.append({"op": "delete", "scope": "primary", "chunk_id": cid,
                            "old_text": by_id[cid]["text"], "label": label(cid),
                            "reason": str(op.get("reason") or "")}); targeted.add(cid)
        elif kind == "add":
            new_text = (op.get("new_text") or "").strip()
            if not new_text:
                continue
            after = op.get("after_chunk_id")
            after = after if after in by_id else None
            ops_out.append({"op": "add", "scope": "primary", "new_text": new_text, "after_chunk_id": after,
                            "req_id": (op.get("req_id") or None),
                            "label": after and (by_id[after]["req_id"] or f"#{after}"),
                            "reason": str(op.get("reason") or "")})

    # related = model's related reasons, then EVERY remaining affected hit (so the
    # deterministic set is fully covered — nothing is silently dropped).
    reason_by_id = {}
    for rel in (plan.get("related") if isinstance(plan.get("related"), list) else []):
        if isinstance(rel, dict) and rel.get("chunk_id") in by_id:
            reason_by_id[rel["chunk_id"]] = str(rel.get("reason") or "")
    related_out = []
    for h in hits:
        cid = h["chunk_id"]
        if cid in targeted:
            continue
        related_out.append({"chunk_id": cid, "label": h["req_id"] or h["section"] or f"#{cid}",
                            "text": h["text"], "reason": reason_by_id.get(cid, "Related to the change — review if it should be updated.")})

    return {"refined": refined, "operations": ops_out, "related": related_out}


def propose_scope_edit(owner_id: int, chunk_id: int, change: str, route: "GenRoute | None" = None) -> dict:
    """On-demand: generate a precise edit for ONE touched requirement (no persistence).

    Returns {op: <edit op>} when a change is warranted, or {none: True, reason} when
    the requirement needs none, or {error} on failure/not-found."""
    change = (change or "").strip()
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """select c.text, c.req_id, c.section from brd_chunk c
               join brd_document d on d.id = c.document_id
               where c.id = %s and d.owner_id = %s""",
            (chunk_id, owner_id),
        )
        row = cur.fetchone()
    if not row:
        return {"error": "requirement not found"}
    old, req_id, section = row[0] or "", row[1], row[2]
    label = req_id or section or f"#{chunk_id}"
    prompt = (_SCOPE_EDIT_PROMPT.replace("{change}", change or "(no change described)")
              .replace("{requirement}", _norm(old)[:1200]))
    try:
        d = _parse_json(_gen([{"role": "user", "content": prompt}], route, max_tokens=700))
    except Exception as e:  # noqa: BLE001
        return {"error": f"Couldn't propose an edit: {_err(e)}"}
    if d.get("action") == "edit":
        eo = _edit_op(chunk_id, old, d.get("find") or "", d.get("replace") or "",
                      d.get("reason") or "", label, "consistency")
        if eo:
            return {"op": eo}
    return {"none": True, "reason": str(d.get("reason") or "No change needed to stay consistent.")}


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

"""Generate USE CASES from a BRD, organized as a folder tree.

Design (mirrors the app's existing generation discipline):
  * DETERMINISTIC scope grouping — requirements are grouped by their `section`, in
    document order, so the tree is stable across runs.
  * PER-SCOPE generation — one LLM call per scope (temp 0), so a large BRD stays
    within a free model's context + rate limits. The whole-BRD "batch" is just these
    calls run in sequence; a scope already generated is skipped (resumable).
  * GROUNDED + CITED — use cases derive only from the scope's requirements, and each
    records the requirement chunk_ids it came from (traceability).
  * CACHED — per (owner, project, scope, requirements-hash, model), so re-runs are
    cheap and spend no budget; busted when the BRD's chunks change.

Tree shape (2 levels): scope folder (top) -> optional sub-theme folder -> use cases.
Everything is editable afterwards (see the CRUD helpers).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from psycopg.types.json import Jsonb

from .. import cache
from ..config import settings
from ..db import pool
from ..ingest.edit import list_requirements
from . import engine

log = logging.getLogger("brd.usecases")

_CACHE_NS = "usecases"
_MAX_REQ_CHARS = 500          # per-requirement text budget in the prompt
_GEN_MAX_TOKENS = 8000        # a batch's use-case JSON is large; too small truncates it (unparseable)
_SCOPE_BATCH = 15             # requirements per LLM call — bounds prompt + output so a big scope
                              # (e.g. a 300-requirement BRD with no sections) doesn't overflow one call

_PROMPT = """You are a QA analyst deriving USE CASES from a Business Requirements Document.
Using ONLY the requirements below (all from the scope "{scope}"), produce concrete, testable
use cases. Do NOT invent features that aren't supported by these requirements.

For each use case provide:
- title: short imperative name
- description: what it accomplishes and why
- roles: the actors involved (array of short role names)
- preconditions: what must be true before it starts
- steps: ordered steps to perform (array of short strings)
- expected_behaviour: the expected result / system behaviour
- source_chunk_ids: the chunk_id integer(s) from the list below this use case derives from
- subtheme: an optional short sub-folder name to group related use cases (use "" if none)

REQUIREMENTS (scope: {scope}):
{requirements}

Return ONLY JSON (no prose, no code fences):
{"use_cases": [
  {"subtheme": "", "title": "...", "description": "...", "roles": ["..."],
   "preconditions": "...", "steps": ["..."], "expected_behaviour": "...", "source_chunk_ids": [123]}
]}
"""


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _parse_json(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(0))
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def _group_by_scope(reqs: list[dict]) -> list[tuple[str, list[dict]]]:
    """Requirements grouped by section, preserving first-seen (document) order."""
    order: list[str] = []
    groups: dict[str, list[dict]] = {}
    for r in reqs:
        scope = _norm(r.get("section")) or "General"
        if scope not in groups:
            groups[scope] = []
            order.append(scope)
        groups[scope].append(r)
    return [(s, groups[s]) for s in order]


def _reqs_hash(items: list[dict]) -> str:
    h = hashlib.sha256()
    for it in items:
        h.update(f"{it['chunk_id']}\x00{it.get('text') or ''}\x00".encode("utf-8"))
    return h.hexdigest()


def _candidate_lines(items: list[dict]) -> str:
    out = []
    for it in items:
        label = it.get("req_id") or it.get("section") or f"#{it['chunk_id']}"
        out.append(f"- chunk_id {it['chunk_id']} [{label}]: {_norm(it.get('text'))[:_MAX_REQ_CHARS]}")
    return "\n".join(out)


def _clean_use_cases(raw: dict, allowed_ids: set[int]) -> list[dict]:
    """Validate/normalize the model output; keep only citations to provided chunks."""
    items = raw.get("use_cases") if isinstance(raw.get("use_cases"), list) else []
    out: list[dict] = []
    for uc in items:
        if not isinstance(uc, dict):
            continue
        title = _norm(uc.get("title"))
        if not title:
            continue
        roles = [_norm(x) for x in uc.get("roles", []) if _norm(x)] if isinstance(uc.get("roles"), list) else []
        steps = [_norm(x) for x in uc.get("steps", []) if _norm(x)] if isinstance(uc.get("steps"), list) else []
        srcs = [int(x) for x in uc.get("source_chunk_ids", [])
                if isinstance(x, (int, str)) and str(x).isdigit() and int(x) in allowed_ids] \
            if isinstance(uc.get("source_chunk_ids"), list) else []
        out.append({
            "subtheme": _norm(uc.get("subtheme")),
            "title": title,
            "description": _norm(uc.get("description")),
            "roles": roles,
            "preconditions": _norm(uc.get("preconditions")),
            "steps": steps,
            "expected_behaviour": _norm(uc.get("expected_behaviour")),
            "source_chunk_ids": srcs,
        })
    return out


def _generate_batch(owner_id: int, project: str, scope: str, items: list[dict],
                    model: str | None) -> list[dict]:
    """One LLM call over a bounded batch of a scope's requirements (cached,
    project-tagged so a BRD change can bust it). No DB held during the network call."""
    key = [owner_id, project, scope, _reqs_hash(items), model or settings.gen_model or ""]
    hit = cache.get(_CACHE_NS, key)
    if hit:   # a non-empty cached result; ignore an empty one so a fixed run can retry
        return hit
    prompt = _PROMPT.replace("{scope}", scope).replace("{requirements}", _candidate_lines(items))
    text = engine.complete_chat([{"role": "user", "content": prompt}],
                                model=model, temperature=0.0, max_tokens=_GEN_MAX_TOKENS)
    ucs = _clean_use_cases(_parse_json(text), {it["chunk_id"] for it in items})
    if ucs:
        cache.set(_CACHE_NS, key, ucs, owner_id=owner_id, project=project)
    return ucs


# --- persistence -----------------------------------------------------------

def has_use_cases(owner_id: int, project: str) -> bool:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select 1 from use_case_folder where owner_id = %s and project = %s limit 1",
                    (owner_id, project))
        return cur.fetchone() is not None


def clear(owner_id: int, project: str) -> None:
    """Delete all folders + use cases for a project (folders cascade to use cases)."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("delete from use_case_folder where owner_id = %s and project = %s", (owner_id, project))
        conn.commit()


def _next_uc_number(owner_id: int, project: str) -> int:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from use_case where owner_id = %s and project = %s", (owner_id, project))
        return (cur.fetchone()[0] or 0) + 1


def _folder(cur, owner_id: int, project: str, parent_id, name: str) -> int:
    """Look up a folder by (owner, project, parent, name) case-insensitively; create it
    if missing. Looking up fresh in the batch's own transaction (rather than trusting an
    in-memory id) is robust to prior/partial runs and dedups sub-themes the model spells
    with different casing across batches."""
    cur.execute(
        "select id from use_case_folder where owner_id=%s and project=%s and lower(name)=lower(%s) "
        "and parent_id is not distinct from %s order by id limit 1",
        (owner_id, project, name, parent_id),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("select count(*) from use_case_folder where owner_id=%s and project=%s "
                "and parent_id is not distinct from %s", (owner_id, project, parent_id))
    ordinal = cur.fetchone()[0]
    cur.execute(
        "insert into use_case_folder (owner_id, project, parent_id, name, ordinal) "
        "values (%s, %s, %s, %s, %s) returning id",
        (owner_id, project, parent_id, name, ordinal),
    )
    return cur.fetchone()[0]


def _write_batch(owner_id: int, project: str, scope: str, ucs: list[dict], uc_start: int) -> int:
    """Write one batch's use cases under their scope folder + sub-theme subfolders, all
    resolved fresh (look-up-or-create) inside this transaction. Returns count written."""
    n = uc_start
    with pool().connection() as conn, conn.cursor() as cur:
        sf = _folder(cur, owner_id, project, None, scope)
        for uc in ucs:
            fid = _folder(cur, owner_id, project, sf, uc["subtheme"]) if uc["subtheme"] else sf
            cur.execute(
                "insert into use_case (owner_id, project, folder_id, uc_id, title, description, "
                "roles, preconditions, steps, expected_behaviour, source_chunk_ids, ordinal) "
                "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (owner_id, project, fid, f"UC-{n}", uc["title"], uc["description"],
                 Jsonb(uc["roles"]), uc["preconditions"], Jsonb(uc["steps"]),
                 uc["expected_behaviour"], Jsonb(uc["source_chunk_ids"]), n),
            )
            n += 1
        conn.commit()
    return n - uc_start


def generate_for_project(owner_id: int, project: str, *, model: str | None = None,
                         replace: bool = False, on_progress=None) -> dict:
    """Generate the full use-case tree for a BRD. Each scope's requirements are split
    into bounded BATCHES (one LLM call each) so a large scope — e.g. a 300-requirement
    BRD with no sections — doesn't overflow a single call and silently return nothing.

    replace=True clears the existing tree first; otherwise a non-empty tree is left as-is
    (the caller regenerates with replace). Per-batch errors are collected, not fatal, so a
    rate-limited/failed batch doesn't lose the rest. Progress is reported per batch."""
    reqs = list_requirements(owner_id, project)
    if not reqs:
        return {"error": "This BRD has no requirements to derive use cases from."}
    if replace:
        clear(owner_id, project)
    elif has_use_cases(owner_id, project):
        return {"skipped": True, "use_cases": 0, "errors": []}

    groups = _group_by_scope(reqs)
    batches: list[tuple[str, int, list[dict]]] = []   # (scope, scope_ordinal, items)
    for si, (scope, items) in enumerate(groups):
        for bi in range(0, len(items), _SCOPE_BATCH):
            batches.append((scope, si, items[bi:bi + _SCOPE_BATCH]))
    total = len(batches)

    uc_counter = _next_uc_number(owner_id, project)
    made = 0
    errors: list[dict] = []
    for i, (scope, _si, items) in enumerate(batches):
        if on_progress:
            on_progress(i, total, scope)
        try:
            ucs = _generate_batch(owner_id, project, scope, items, model)
            if not ucs:
                continue
            written = _write_batch(owner_id, project, scope, ucs, uc_counter)
            uc_counter += written
            made += written
        except Exception as e:  # noqa: BLE001 — a failed batch (LLM or write) must not lose the rest
            log.warning("use-case batch failed (scope %r): %s", scope, e)
            errors.append({"scope": scope, "error": str(e)})
    if on_progress:
        on_progress(total, total, None)
    return {"scopes": len(groups), "batches": total, "use_cases": made, "errors": errors}


# --- read (tree) -----------------------------------------------------------

def get_tree(owner_id: int, project: str) -> dict:
    """The folder tree with use cases nested under their folders."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "select id, parent_id, name, ordinal from use_case_folder "
            "where owner_id = %s and project = %s order by parent_id nulls first, ordinal, id",
            (owner_id, project),
        )
        folders = [{"id": r[0], "parent_id": r[1], "name": r[2], "ordinal": r[3], "children": [], "use_cases": []}
                   for r in cur.fetchall()]
        cur.execute(
            "select id, folder_id, uc_id, title, description, roles, preconditions, steps, "
            "expected_behaviour, source_chunk_ids, ordinal, status from use_case "
            "where owner_id = %s and project = %s order by folder_id, ordinal, id",
            (owner_id, project),
        )
        ucs = [{"id": r[0], "folder_id": r[1], "uc_id": r[2], "title": r[3], "description": r[4],
                "roles": r[5], "preconditions": r[6], "steps": r[7], "expected_behaviour": r[8],
                "source_chunk_ids": r[9], "ordinal": r[10], "status": r[11]} for r in cur.fetchall()]
    by_id = {f["id"]: f for f in folders}
    for uc in ucs:
        f = by_id.get(uc["folder_id"])
        if f:
            f["use_cases"].append(uc)
    roots = []
    for f in folders:
        parent = by_id.get(f["parent_id"])
        (parent["children"] if parent else roots).append(f)
    return {"project": project, "folders": roots}

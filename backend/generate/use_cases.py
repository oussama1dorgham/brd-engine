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
from .. import use_case_versioning as ucv
from ..config import settings
from ..db import pool
from ..ingest.edit import list_requirements
from . import engine
from .change_agent import GenRoute

log = logging.getLogger("brd.usecases")

_CACHE_NS = "usecases"
_MAX_REQ_CHARS = 500          # per-requirement text budget in the prompt
_GEN_MAX_TOKENS = 8000        # a batch's use-case JSON is large; too small truncates it (unparseable)
_SCOPE_BATCH = 15             # requirements per LLM call — bounds prompt + output so a big scope
                              # (e.g. a 300-requirement BRD with no sections) doesn't overflow one call
_CATS_PER = 7                 # ~1 category per this many requirements (for a large scope's taxonomy)
_CATS_MIN, _CATS_MAX = 4, 12  # clamp the category count so folders stay bounded
_MAX_SUBTHEMES_SMALL = 5      # a single-batch scope groups into at most this many sub-themes
_OTHER = "Other"             # catch-all folder so no use case is ever left uncategorized

_PROMPT = """You are a QA analyst deriving USE CASES from a Business Requirements Document.
Using ONLY the requirements below (all from the scope "{scope}"), produce concrete, testable
use cases. Do NOT invent features that aren't supported by these requirements.

Every use case MUST be complete — omit any use case you cannot fully specify. For each one provide:
- title: short imperative name
- description: what it accomplishes and why
- roles: the actors involved (array of short role names, at least one)
- preconditions: what must be true before it starts
- steps: ordered steps to perform (array of at least one short string)
- expected_behaviour: the expected result / system behaviour (required, non-empty)
- source_chunk_ids: the chunk_id integer(s) from the list below this use case derives from (at least one)
- subtheme: {subtheme_rule}

REQUIREMENTS (scope: {scope}):
{requirements}

Return ONLY JSON (no prose, no code fences):
{"use_cases": [
  {"subtheme": "...", "title": "...", "description": "...", "roles": ["..."],
   "preconditions": "...", "steps": ["..."], "expected_behaviour": "...", "source_chunk_ids": [123]}
]}
"""

_SUBTHEME_FREE = (f"a short sub-folder name grouping related use cases — use at most about "
                  f"{_MAX_SUBTHEMES_SMALL} distinct sub-themes for this scope, and never leave it blank")

_TAXONOMY_PROMPT = """You are organizing a Business Requirements Document into folders for the scope
"{scope}". From the requirements below, propose EXACTLY {n} concise, broad, non-overlapping category
names that together cover them (folders a QA engineer would use). Prefer reusable groupings over narrow
ones; no duplicates.

REQUIREMENTS:
{requirements}

Return ONLY JSON (no prose, no code fences): {"categories": ["...", "..."]}
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


def _clean_use_cases(raw: dict, allowed_ids: set[int], categories: list[str] | None = None) -> list[dict]:
    """Validate + normalize the model output. Drops MALFORMED use cases (a QA card must
    have a title, >=1 step, an expected behaviour, and >=1 real requirement citation).
    Coerces the folder: with a fixed `categories` list, snap subtheme to a matching
    category (case-insensitive) else 'Other'; otherwise use the model's sub-theme,
    defaulting blanks to 'Other' so nothing is ever left uncategorized."""
    cat_by_lower = {c.lower(): c for c in (categories or [])}
    items = raw.get("use_cases") if isinstance(raw.get("use_cases"), list) else []
    out: list[dict] = []
    for uc in items:
        if not isinstance(uc, dict):
            continue
        title = _norm(uc.get("title"))
        steps = [_norm(x) for x in uc.get("steps", []) if _norm(x)] if isinstance(uc.get("steps"), list) else []
        expected = _norm(uc.get("expected_behaviour"))
        srcs = [int(x) for x in uc.get("source_chunk_ids", [])
                if isinstance(x, (int, str)) and str(x).isdigit() and int(x) in allowed_ids] \
            if isinstance(uc.get("source_chunk_ids"), list) else []
        srcs = list(dict.fromkeys(srcs))                      # dedupe, keep order
        if not (title and steps and expected and srcs):        # malformed → drop
            continue
        roles = [_norm(x) for x in uc.get("roles", []) if _norm(x)] if isinstance(uc.get("roles"), list) else []
        sub = _norm(uc.get("subtheme"))
        if categories is not None:
            sub = cat_by_lower.get(sub.lower(), _OTHER)         # snap to the fixed taxonomy
        elif not sub:
            sub = _OTHER
        out.append({
            "subtheme": sub,
            "title": title,
            "description": _norm(uc.get("description")),
            "roles": roles,
            "preconditions": _norm(uc.get("preconditions")),
            "steps": steps,
            "expected_behaviour": expected,
            "source_chunk_ids": srcs,
        })
    return out


def _derive_categories(owner_id: int, project: str, scope: str, items: list[dict],
                       route: GenRoute | None) -> list[str]:
    """Pass 1 for a large scope: derive a FIXED, bounded taxonomy of category names from
    the requirements (cached). Every batch then classifies into this same list, so folders
    stay consistent and few instead of each batch inventing its own."""
    n = max(_CATS_MIN, min(_CATS_MAX, -(-len(items) // _CATS_PER)))
    r = route or GenRoute()
    key = ["cats", owner_id, project, scope, _reqs_hash(items), n, r.model or settings.gen_model or ""]
    hit = cache.get(_CACHE_NS, key)
    if hit:
        return hit
    prompt = (_TAXONOMY_PROMPT.replace("{scope}", scope).replace("{n}", str(n))
              .replace("{requirements}", _candidate_lines(items)))
    text = engine.complete_chat([{"role": "user", "content": prompt}],
                                provider=r.provider, api_key=r.api_key, base_url=r.base_url,
                                model=r.model, temperature=0.0, max_tokens=800)
    d = _parse_json(text)
    raw = d.get("categories") if isinstance(d.get("categories"), list) else []
    cats: list[str] = []
    seen: set[str] = set()
    for c in raw:
        c = _norm(c)
        if c and c.lower() not in seen:
            seen.add(c.lower())
            cats.append(c)
    cats = cats[:_CATS_MAX]
    if cats:
        cache.set(_CACHE_NS, key, cats, owner_id=owner_id, project=project)
    return cats


def _generate_batch(owner_id: int, project: str, scope: str, items: list[dict],
                    route: GenRoute | None, categories: list[str] | None = None) -> list[dict]:
    """One LLM call over a bounded batch of a scope's requirements → validated use cases
    (cached, project-tagged). With `categories`, each use case is classified into that fixed
    taxonomy; otherwise the model free-groups (single-batch scopes). No DB held here."""
    r = route or GenRoute()
    key = [owner_id, project, scope, _reqs_hash(items), r.model or settings.gen_model or "",
           categories or "free"]
    hit = cache.get(_CACHE_NS, key)
    if hit:   # a non-empty cached result; ignore an empty one so a fixed run can retry
        return hit
    if categories:
        rule = ("assign this use case to EXACTLY ONE of these categories, copied verbatim: "
                + "; ".join(categories) + f'. If none fits, use "{_OTHER}". Never leave it blank.')
    else:
        rule = _SUBTHEME_FREE
    prompt = (_PROMPT.replace("{scope}", scope).replace("{subtheme_rule}", rule)
              .replace("{requirements}", _candidate_lines(items)))
    text = engine.complete_chat([{"role": "user", "content": prompt}],
                                provider=r.provider, api_key=r.api_key, base_url=r.base_url,
                                model=r.model, temperature=0.0, max_tokens=_GEN_MAX_TOKENS)
    ucs = _clean_use_cases(_parse_json(text), {it["chunk_id"] for it in items}, categories)
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
    """Delete all folders + use cases for a project (folders cascade to use cases),
    and the batch ledger — so a subsequent run regenerates every batch from scratch
    instead of skipping ones marked 'done' by the run being replaced."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("delete from use_case_folder where owner_id = %s and project = %s", (owner_id, project))
        cur.execute("delete from use_case_batch where owner_id = %s and project = %s", (owner_id, project))
        conn.commit()


# --- resume ledger (per-batch, durable — survives a crash/restart) ----------

def _mark_batch(owner_id: int, project: str, scope: str, batch_hash: str,
                status: str, uc_count: int, error: str | None = None) -> None:
    """Record a batch's outcome (its own short transaction). Used for FAILED batches
    (LLM error / truncation) and for batches whose use cases already existed. A
    successful write records 'done' inside `_write_batch`'s own transaction instead,
    so the use cases and the 'done' mark commit atomically."""
    with pool().connection() as conn, conn.cursor() as cur:
        _upsert_batch(cur, owner_id, project, scope, batch_hash, status, uc_count, error)
        conn.commit()


def _upsert_batch(cur, owner_id: int, project: str, scope: str, batch_hash: str,
                  status: str, uc_count: int, error: str | None) -> None:
    cur.execute(
        "insert into use_case_batch (owner_id, project, scope, batch_hash, status, uc_count, error) "
        "values (%s,%s,%s,%s,%s,%s,%s) "
        "on conflict (owner_id, project, batch_hash) do update set "
        "status=excluded.status, uc_count=excluded.uc_count, error=excluded.error, updated_at=now()",
        (owner_id, project, scope, batch_hash, status, uc_count, error),
    )


def _done_hashes(cur, owner_id: int, project: str) -> set[str]:
    cur.execute("select batch_hash from use_case_batch "
                "where owner_id=%s and project=%s and status='done'", (owner_id, project))
    return {r[0] for r in cur.fetchall()}


def _existing_titles(cur, owner_id: int, project: str) -> dict[str, set[str]]:
    """Existing use-case titles grouped by their SCOPE (the top-level folder name).
    Seeds the per-scope dedupe from the DB so resume never re-inserts a use case that
    a prior run already wrote — correctness even for trees generated before the ledger
    existed. The tree is 2 levels, so a use case's scope is its folder's parent name
    (sub-theme case) or the folder's own name (folder is the scope itself)."""
    cur.execute(
        "select coalesce(pf.name, f.name) as scope, lower(uc.title) "
        "from use_case uc "
        "join use_case_folder f on f.id = uc.folder_id "
        "left join use_case_folder pf on pf.id = f.parent_id "
        "where uc.owner_id=%s and uc.project=%s",
        (owner_id, project),
    )
    out: dict[str, set[str]] = {}
    for scope, title in cur.fetchall():
        out.setdefault(scope, set()).add(title)
    return out


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


def _write_batch(owner_id: int, project: str, scope: str, ucs: list[dict], uc_start: int,
                 batch_hash: str, changed_by: int | None = None) -> int:
    """Write one batch's use cases under their scope folder + sub-theme subfolders, all
    resolved fresh (look-up-or-create) inside this transaction, and record the batch as
    'done' in the SAME transaction. Atomicity is what makes resume safe: either the use
    cases and the 'done' mark both commit, or neither does — a crash never leaves written
    use cases the ledger has no record of (which would be re-generated as duplicates).
    Each card's `batch_hash` is stored (links it to its batch for incremental sync) and a
    version-1 'create' history row is recorded in the same tx. Returns count written."""
    n = uc_start
    with pool().connection() as conn, conn.cursor() as cur:
        sf = _folder(cur, owner_id, project, None, scope)
        for uc in ucs:
            fid = _folder(cur, owner_id, project, sf, uc["subtheme"]) if uc["subtheme"] else sf
            cur.execute(
                "insert into use_case (owner_id, project, folder_id, uc_id, title, description, "
                "roles, preconditions, steps, expected_behaviour, source_chunk_ids, ordinal, batch_hash) "
                "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id, uid",
                (owner_id, project, fid, f"UC-{n}", uc["title"], uc["description"],
                 Jsonb(uc["roles"]), uc["preconditions"], Jsonb(uc["steps"]),
                 uc["expected_behaviour"], Jsonb(uc["source_chunk_ids"]), n, batch_hash),
            )
            uc_pk, uid = cur.fetchone()
            folder_path = f"{scope} / {uc['subtheme']}" if uc["subtheme"] else scope
            ucv._record(cur, uid=uid, use_case_id=uc_pk, owner_id=owner_id, project=project,
                        uc_id=f"UC-{n}", kind="create",
                        data={"title": uc["title"], "description": uc["description"],
                              "roles": uc["roles"], "preconditions": uc["preconditions"],
                              "steps": uc["steps"], "expected_behaviour": uc["expected_behaviour"],
                              "source_chunk_ids": uc["source_chunk_ids"], "status": "draft",
                              "batch_hash": batch_hash, "folder_path": folder_path},
                        changed_by=changed_by if changed_by is not None else owner_id,
                        summary="Created")
            n += 1
        _upsert_batch(cur, owner_id, project, scope, batch_hash, "done", n - uc_start, None)
        conn.commit()
    return n - uc_start


def _batches(reqs: list[dict]) -> list[tuple[str, list[dict], str]]:
    """The deterministic (scope, items, batch_hash) work list. Grouping and splitting are
    pure functions of the requirements, so batch N is the same set — and the same hash —
    on every run, which is what lets the ledger skip the ones already done."""
    groups = _group_by_scope(reqs)
    return [(scope, items[bi:bi + _SCOPE_BATCH], _reqs_hash(items[bi:bi + _SCOPE_BATCH]))
            for scope, items in groups
            for bi in range(0, len(items), _SCOPE_BATCH)]


def resume_state(owner_id: int, project: str) -> dict:
    """Ledger-derived progress for a project's use-case generation, computed on demand so
    it survives a server restart (unlike the in-memory job). Lets the UI offer Resume vs
    Regenerate: `pending > 0 and done > 0` ⇒ a partial run to continue."""
    reqs = list_requirements(owner_id, project)
    if not reqs:
        return {"exists": False, "total": 0, "done": 0, "failed": 0, "pending": 0,
                "complete": False, "resumable": False}
    hashes = [h for _, _, h in _batches(reqs)]
    total = len(hashes)
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select batch_hash, status from use_case_batch where owner_id=%s and project=%s",
                    (owner_id, project))
        ledger = {r[0]: r[1] for r in cur.fetchall()}
    done = sum(1 for h in hashes if ledger.get(h) == "done")
    failed = sum(1 for h in hashes if ledger.get(h) == "failed")
    pending = total - done                       # failed counts as pending (retried on resume)
    return {"exists": has_use_cases(owner_id, project), "total": total, "done": done,
            "failed": failed, "pending": pending, "complete": pending == 0,
            "resumable": done > 0 and pending > 0}


def generate_for_project(owner_id: int, project: str, *, route: GenRoute | None = None,
                         replace: bool = False, mode: str | None = None, on_progress=None) -> dict:
    """Generate the use-case tree for a BRD, one bounded BATCH (LLM call) per slice of a
    scope's requirements, so a large scope doesn't overflow a single call.

    `route` is the generation route (the user's BYOK provider + chosen model); None falls
    back to the system OpenRouter + GEN_MODEL. The model id is part of every cache key.

    RESUMABLE. `mode` (falls back to "replace" if `replace` else "resume"):
      * "resume" (default) — skip batches the ledger marks 'done' (already generated and
        written in a prior run) and process only the rest, so a run stopped by a quota
        limit or outage continues from where it left off. A fully-done tree yields nothing.
      * "replace" — clear the tree + ledger first, then regenerate every batch.
    Per-batch failures are recorded ('failed' ledger row) and collected, never fatal, so a
    rate-limited/truncated batch is retried on the next resume and never loses the rest.
    Duplicates are prevented two ways: 'done' batches are skipped, and the per-scope title
    dedupe is seeded from the DB, so a re-run of an unrecorded batch still can't re-insert
    an existing use case. Progress is reported per batch."""
    reqs = list_requirements(owner_id, project)
    if not reqs:
        return {"error": "This BRD has no requirements to derive use cases from."}
    mode = mode or ("replace" if replace else "resume")
    if mode == "replace":
        clear(owner_id, project)

    groups = _group_by_scope(reqs)
    groups_dict = dict(groups)
    batches = _batches(reqs)
    total = len(batches)

    with pool().connection() as conn, conn.cursor() as cur:
        done_hashes = _done_hashes(cur, owner_id, project)          # skip these (already written)
        seen_titles = _existing_titles(cur, owner_id, project)      # DB-seeded dedupe (correctness)

    uc_counter = _next_uc_number(owner_id, project)
    scope_cats: dict[str, list[str] | None] = {}   # fixed taxonomy per large scope
    made = 0
    skipped = 0
    errors: list[dict] = []
    for i, (scope, items, bhash) in enumerate(batches):
        if on_progress:
            on_progress(i, total, scope)
        if bhash in done_hashes:                   # generated + written by a prior run → resume past it
            skipped += 1
            continue
        # Pass 1 (once per LARGE scope): derive a fixed taxonomy; small scopes free-group.
        if scope not in scope_cats:
            cats = None
            if len(groups_dict[scope]) > _SCOPE_BATCH:
                try:
                    cats = _derive_categories(owner_id, project, scope, groups_dict[scope], route) or None
                except Exception as e:  # noqa: BLE001 — fall back to free grouping
                    log.warning("taxonomy failed (scope %r): %s", scope, e)
            scope_cats[scope] = cats
        try:
            ucs = _generate_batch(owner_id, project, scope, items, route, scope_cats[scope])
        except Exception as e:  # noqa: BLE001 — a failed batch must not lose the rest
            log.warning("use-case batch failed (scope %r): %s", scope, e)
            _mark_batch(owner_id, project, scope, bhash, "failed", 0, f"{type(e).__name__}: {e}")
            errors.append({"scope": scope, "error": str(e)})
            continue
        if not ucs:                                # empty parse / truncated JSON → retry on resume
            log.warning("use-case batch produced no valid use cases (scope %r)", scope)
            _mark_batch(owner_id, project, scope, bhash, "failed", 0,
                        "no valid use cases parsed (possible truncation)")
            errors.append({"scope": scope, "error": "no valid use cases parsed"})
            continue
        seen = seen_titles.setdefault(scope, set())
        fresh = []
        for uc in ucs:
            k = uc["title"].lower()
            if k not in seen:
                seen.add(k)
                fresh.append(uc)
        if not fresh:                              # all already present → the batch is effectively done
            _mark_batch(owner_id, project, scope, bhash, "done", 0, None)
            continue
        try:
            written = _write_batch(owner_id, project, scope, fresh, uc_counter, bhash)
            uc_counter += written
            made += written
        except Exception as e:  # noqa: BLE001
            log.warning("use-case write failed (scope %r): %s", scope, e)
            _mark_batch(owner_id, project, scope, bhash, "failed", 0, f"{type(e).__name__}: {e}")
            errors.append({"scope": scope, "error": str(e)})
    if on_progress:
        on_progress(total, total, None)
    return {"scopes": len(groups), "batches": total, "use_cases": made, "skipped": skipped,
            "mode": mode, "errors": errors}


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


# --- editing (QA refines the generated tree; all owner-scoped) --------------

_UC_TEXT = ("title", "description", "preconditions", "expected_behaviour")
_UC_JSON = ("roles", "steps")


def update_use_case(owner_id: int, uc_pk: int, fields: dict) -> bool:
    """Edit whitelisted fields of one use case (owner-scoped)."""
    sets, vals = [], []
    for k in _UC_TEXT:
        if k in fields:
            sets.append(f"{k} = %s")
            vals.append(_norm(str(fields[k] or "")) if k == "title" else str(fields[k] or ""))
    for k in _UC_JSON:
        if k in fields:
            v = fields[k] if isinstance(fields[k], list) else []
            sets.append(f"{k} = %s")
            vals.append(Jsonb([str(x) for x in v]))
    if not sets:
        return False
    sets.append("updated_at = now()")
    vals += [uc_pk, owner_id]
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(f"update use_case set {', '.join(sets)} where id = %s and owner_id = %s", vals)
        ok = cur.rowcount > 0
        if ok:
            ucv.record_change(cur, uc_pk, "edit", owner_id)   # snapshot new state (audit + backup)
        conn.commit()
    return ok


def delete_use_case(owner_id: int, uc_pk: int) -> bool:
    with pool().connection() as conn, conn.cursor() as cur:
        ucv.record_change(cur, uc_pk, "delete", owner_id)     # backup before removal (recoverable in Trash)
        cur.execute("delete from use_case where id = %s and owner_id = %s", (uc_pk, owner_id))
        ok = cur.rowcount > 0
        conn.commit()
    return ok


def move_use_case(owner_id: int, uc_pk: int, folder_id: int) -> bool:
    """Move a use case into another of the owner's folders (same project enforced)."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update use_case set folder_id = %s, updated_at = now() where id = %s and owner_id = %s "
            "and exists (select 1 from use_case_folder f where f.id = %s and f.owner_id = %s "
            "and f.project = use_case.project)",
            (folder_id, uc_pk, owner_id, folder_id, owner_id),
        )
        ok = cur.rowcount > 0
        if ok:
            ucv.record_change(cur, uc_pk, "move", owner_id)   # audit the folder move
        conn.commit()
    return ok


def create_folder(owner_id: int, project: str, parent_id: int | None, name: str) -> dict | None:
    name = _norm(name)
    if not name:
        return None
    with pool().connection() as conn, conn.cursor() as cur:
        if parent_id is not None:
            cur.execute("select 1 from use_case_folder where id = %s and owner_id = %s and project = %s",
                        (parent_id, owner_id, project))
            if not cur.fetchone():
                return None
        fid = _folder(cur, owner_id, project, parent_id, name)
        conn.commit()
    return {"id": fid, "parent_id": parent_id, "name": name}


def rename_folder(owner_id: int, folder_id: int, name: str) -> bool:
    name = _norm(name)
    if not name:
        return False
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update use_case_folder set name = %s where id = %s and owner_id = %s",
                    (name, folder_id, owner_id))
        ok = cur.rowcount > 0
        conn.commit()
    return ok


def delete_folder(owner_id: int, folder_id: int) -> bool:
    """Delete a folder and everything under it (subfolders + use cases cascade)."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("delete from use_case_folder where id = %s and owner_id = %s", (folder_id, owner_id))
        ok = cur.rowcount > 0
        conn.commit()
    return ok

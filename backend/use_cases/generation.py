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

from backend.core import cache
from backend.use_cases import incremental as inc
from backend.use_cases import testing as uct
from backend.use_cases import versioning as ucv
from backend.core.config import settings
from backend.core.db import pool
from backend.requirements.edit import list_requirements
from backend.llm import engine
from backend.requirements.change_agent import GenRoute

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

# Every card is classified so a suite covers each requirement from all three angles.
_CASE_TYPES = ("functional", "negative", "edge")
_CASE_DEFAULT = "functional"

# Coverage is chosen per generation run (see generate_for_project). "functional" reproduces
# the old happy-path-only behaviour; "full" additionally derives negative + edge cases.
_COVERAGE_FUNCTIONAL = ('Generate FUNCTIONAL (happy-path) use cases only — valid inputs and the '
                        'expected successful outcome. Set "case_type" to "functional" for every one.')
_COVERAGE_FULL = ('Be THOROUGH. For each requirement derive not only the FUNCTIONAL happy-path case(s) '
                  'but also, wherever the requirement implies them, NEGATIVE cases (invalid input, an '
                  'unauthorized actor, a violated precondition, error/rejection handling) and EDGE cases '
                  '(boundaries such as empty/zero/maximum/limit values, concurrency, unusual-but-valid '
                  'input, state transitions). Cover a requirement from all three angles when they apply, '
                  'and tag each use case with the correct "case_type". Never fabricate behaviour the '
                  'requirements do not support — omit an angle that genuinely does not apply.')

_PROMPT = """You are a QA analyst deriving USE CASES from a Business Requirements Document.
Using ONLY the requirements below (all from the scope "{scope}"), produce concrete, testable
use cases. Do NOT invent features that aren't supported by these requirements.

{coverage_rule}

Every use case MUST be complete — omit any use case you cannot fully specify. For each one provide:
- case_type: one of "functional" (happy path, valid input, successful outcome), "negative" (invalid
  input, an unauthorized actor, a violated precondition, or error/rejection handling), or "edge"
  (boundaries — empty/zero/maximum/limit values, concurrency, unusual-but-valid input, state transitions)
- title: short imperative name (for a negative/edge case make the abnormal condition explicit in the name)
- description: what it accomplishes and why
- roles: the actors involved (array of short role names, at least one)
- preconditions: what must be true before it starts
- steps: ordered steps to perform (array of at least one short string)
- expected_behaviour: the expected result / system behaviour (required, non-empty; for a negative case
  this is the rejection or error the system must produce, not a success)
- source_chunk_ids: the chunk_id integer(s) from the list below this use case derives from (at least one)
- subtheme: {subtheme_rule}

REQUIREMENTS (scope: {scope}):
{requirements}

Return ONLY JSON (no prose, no code fences):
{"use_cases": [
  {"case_type": "functional", "subtheme": "...", "title": "...", "description": "...", "roles": ["..."],
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
        ctype = _norm(uc.get("case_type")).lower()
        if ctype not in _CASE_TYPES:                            # unknown / blank → happy path
            ctype = _CASE_DEFAULT
        sub = _norm(uc.get("subtheme"))
        if categories is not None:
            sub = cat_by_lower.get(sub.lower(), _OTHER)         # snap to the fixed taxonomy
        elif not sub:
            sub = _OTHER
        out.append({
            "subtheme": sub,
            "case_type": ctype,
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
                    route: GenRoute | None, categories: list[str] | None = None,
                    coverage: str = _CASE_DEFAULT) -> list[dict]:
    """One LLM call over a bounded batch of a scope's requirements → validated use cases
    (cached, project-tagged). With `categories`, each use case is classified into that fixed
    taxonomy; otherwise the model free-groups (single-batch scopes). `coverage` ("functional"
    | "full") is part of the cache key so a functional-only run and a full-analysis run never
    collide. No DB held here."""
    r = route or GenRoute()
    key = [owner_id, project, scope, _reqs_hash(items), r.model or settings.gen_model or "",
           categories or "free", coverage]
    hit = cache.get(_CACHE_NS, key)
    if hit:   # a non-empty cached result; ignore an empty one so a fixed run can retry
        return hit
    if categories:
        rule = ("assign this use case to EXACTLY ONE of these categories, copied verbatim: "
                + "; ".join(categories) + f'. If none fits, use "{_OTHER}". Never leave it blank.')
    else:
        rule = _SUBTHEME_FREE
    coverage_rule = _COVERAGE_FULL if coverage == "full" else _COVERAGE_FUNCTIONAL
    prompt = (_PROMPT.replace("{scope}", scope).replace("{subtheme_rule}", rule)
              .replace("{coverage_rule}", coverage_rule)
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


def clear(owner_id: int, project: str, changed_by: int | None = None) -> None:
    """Delete all folders + use cases for a project (folders cascade to use cases),
    and the batch ledger — so a subsequent run regenerates every batch from scratch
    instead of skipping ones marked 'done' by the run being replaced.

    NON-DESTRUCTIVE: before deleting, capture a whole-tree snapshot (one-click undo of
    the entire regenerate) AND record a per-card 'regenerate' version for every live card
    (so each survives in history and is recoverable from Trash). All in one transaction,
    so the backup and the delete commit together."""
    changed_by = changed_by if changed_by is not None else owner_id
    with pool().connection() as conn, conn.cursor() as cur:
        ucv.snapshot_tree(cur, owner_id, project, changed_by, kind="pre-regenerate",
                          label="Before regenerate")
        cur.execute("select id from use_case where owner_id = %s and project = %s", (owner_id, project))
        for (uc_pk,) in cur.fetchall():
            ucv.record_change(cur, uc_pk, "regenerate", owner_id, changed_by)
        cur.execute("delete from use_case_folder where owner_id = %s and project = %s", (owner_id, project))
        cur.execute("delete from use_case_batch where owner_id = %s and project = %s", (owner_id, project))
        cur.execute("delete from use_case_req_basis where owner_id = %s and project = %s", (owner_id, project))
        conn.commit()


def purge_project(owner_id: int, project: str) -> int:
    """Hard-delete EVERY use-case artifact for a project — folders + cases, the batch
    ledger, version history, and tree snapshots. Used when the BRD itself is deleted, so
    nothing is left orphaned (use-case tables have no FK to brd_document). Unlike clear(),
    this does NOT snapshot first — the whole project is going away. Returns cases removed."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from use_case where owner_id = %s and project = %s", (owner_id, project))
        n = cur.fetchone()[0] or 0
        # history/snapshots first (version.use_case_id is ON DELETE SET NULL, so it wouldn't
        # be removed by the folder cascade), then the tree (folders cascade to use_case).
        cur.execute("delete from use_case_version where owner_id = %s and project = %s", (owner_id, project))
        cur.execute("delete from use_case_tree_snapshot where owner_id = %s and project = %s", (owner_id, project))
        cur.execute("delete from use_case_batch where owner_id = %s and project = %s", (owner_id, project))
        cur.execute("delete from use_case_req_basis where owner_id = %s and project = %s", (owner_id, project))
        cur.execute("delete from use_case_folder where owner_id = %s and project = %s", (owner_id, project))
        keys = uct.purge_project(cur, owner_id, project)   # results, proofs and cycles too
        # legacy change proposals (protected regeneration, retired — the table remains for history)
        cur.execute("delete from use_case_change_proposal where owner_id = %s and project = %s", (owner_id, project))
        conn.commit()
    uct._drop_blobs(keys)                                # objects only after the rows are gone
    return n


def purge_project_rows(cur, owner_id: int, project: str) -> list[str]:
    """Like purge_project, but runs in the CALLER's cursor/transaction — no connection of its
    own, no commit, and it does NOT drop the proof blobs (the caller does that after ITS
    transaction commits). Used by the BRD-delete endpoint so the BRD's documents, its
    use-case tree, and its ingest rows are all removed atomically in one transaction.

    Returns the test-proof object keys (from testing.purge_project) for the caller to delete
    after commit."""
    cur.execute("delete from use_case_version where owner_id = %s and project = %s", (owner_id, project))
    cur.execute("delete from use_case_tree_snapshot where owner_id = %s and project = %s", (owner_id, project))
    cur.execute("delete from use_case_batch where owner_id = %s and project = %s", (owner_id, project))
    cur.execute("delete from use_case_req_basis where owner_id = %s and project = %s", (owner_id, project))
    cur.execute("delete from use_case_folder where owner_id = %s and project = %s", (owner_id, project))
    keys = uct.purge_project(cur, owner_id, project)   # results, proofs and cycles too
    cur.execute("delete from use_case_change_proposal where owner_id = %s and project = %s", (owner_id, project))
    return keys


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
    """Highest UC-N label EVER issued + 1 — live cases AND per-card history — so a code is
    never reused: after the highest case is deleted, its number still belongs to it in every
    published version, report and audit entry."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "select coalesce(max((substring(l from 4))::int), 0) + 1 from ("
            "  select uc_id as l from use_case where owner_id = %s and project = %s"
            "  union all select uc_id from use_case_version where owner_id = %s and project = %s) x "
            "where l ~ '^UC-[0-9]+$'",
            (owner_id, project, owner_id, project),
        )
        return cur.fetchone()[0]


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


def _insert_cards(cur, owner_id: int, project: str, scope: str, ucs: list[dict], uc_start: int,
                  changed_by: int) -> int:
    """Insert new use cases under their scope folder + sub-theme subfolders (looked up or
    created in the caller's transaction), each with its version-1 'create' history row.
    Codes continue from `uc_start` (never reused — see _next_uc_number). Returns count."""
    n = uc_start
    sf = _folder(cur, owner_id, project, None, scope)
    for uc in ucs:
        fid = _folder(cur, owner_id, project, sf, uc["subtheme"]) if uc["subtheme"] else sf
        cur.execute(
            "insert into use_case (owner_id, project, folder_id, uc_id, title, description, "
            "roles, preconditions, steps, expected_behaviour, source_chunk_ids, ordinal, case_type) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id, uid",
            (owner_id, project, fid, f"UC-{n}", uc["title"], uc["description"],
             Jsonb(uc["roles"]), uc["preconditions"], Jsonb(uc["steps"]),
             uc["expected_behaviour"], Jsonb(uc["source_chunk_ids"]), n,
             uc.get("case_type", _CASE_DEFAULT)),
        )
        uc_pk, uid = cur.fetchone()
        folder_path = f"{scope} / {uc['subtheme']}" if uc["subtheme"] else scope
        ucv._record(cur, uid=uid, use_case_id=uc_pk, owner_id=owner_id, project=project,
                    uc_id=f"UC-{n}", kind="create",
                    data={"title": uc["title"], "description": uc["description"],
                          "roles": uc["roles"], "preconditions": uc["preconditions"],
                          "steps": uc["steps"], "expected_behaviour": uc["expected_behaviour"],
                          "source_chunk_ids": uc["source_chunk_ids"], "status": "draft",
                          "case_type": uc.get("case_type", _CASE_DEFAULT), "folder_path": folder_path},
                    changed_by=changed_by, summary="Created")
        n += 1
    return n - uc_start


def _batches(reqs: list[dict]) -> list[tuple[str, list[dict], str]]:
    """The deterministic (scope, items, batch_hash) slicing of ALL requirements — used for
    progress totals and to backfill the requirement basis from the old batch ledger."""
    groups = _group_by_scope(reqs)
    return [(scope, items[bi:bi + _SCOPE_BATCH], _reqs_hash(items[bi:bi + _SCOPE_BATCH]))
            for scope, items in groups
            for bi in range(0, len(items), _SCOPE_BATCH)]


# --- requirement basis (incremental generation — migration 033) ------------

def _basis(cur, owner_id: int, project: str) -> dict[int, str]:
    cur.execute("select chunk_id, hash from use_case_req_basis where owner_id=%s and project=%s",
                (owner_id, project))
    return dict(cur.fetchall())


def _set_basis(cur, owner_id: int, project: str, items: list[dict]) -> None:
    """Record requirements as generated-from at their current text (caller's transaction)."""
    if items:
        cur.executemany(
            "insert into use_case_req_basis (owner_id, project, chunk_id, hash) values (%s,%s,%s,%s) "
            "on conflict (owner_id, project, chunk_id) do update set hash=excluded.hash, updated_at=now()",
            [(owner_id, project, r["chunk_id"], inc.req_hash(r["text"])) for r in items])


def _ensure_basis(owner_id: int, project: str, reqs: list[dict]) -> None:
    """A BRD generated before the basis existed: backfill it once, from (a) every requirement
    of a batch the old ledger marks done and (b) every requirement a live case cites — both at
    their CURRENT text (they were generated from it, or are covered). The rest stays pending."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select 1 from use_case_req_basis where owner_id=%s and project=%s limit 1", (owner_id, project))
        if cur.fetchone():
            return
        cur.execute("select source_chunk_ids from use_case where owner_id=%s and project=%s", (owner_id, project))
        rows = cur.fetchall()
        if not rows:
            return
        cited = {int(c) for (cids,) in rows for c in (cids or []) if str(c).isdigit()}
        cur.execute("select batch_hash from use_case_batch where owner_id=%s and project=%s and status='done'",
                    (owner_id, project))
        done = {r[0] for r in cur.fetchall()}
        keep = {it["chunk_id"]: it for _, items, h in _batches(reqs) if h in done for it in items}
        keep.update({r["chunk_id"]: r for r in reqs if r["chunk_id"] in cited})
        _set_basis(cur, owner_id, project, list(keep.values()))
        conn.commit()


def _cards(cur, owner_id: int, project: str) -> list[dict]:
    cur.execute("select id, uid, uc_id, title, description, roles, preconditions, steps, expected_behaviour, "
                "source_chunk_ids, case_type, folder_id from use_case where owner_id=%s and project=%s "
                "order by ordinal, id", (owner_id, project))
    return [{"id": r[0], "uid": r[1], "uc_id": r[2], "title": r[3], "description": r[4], "roles": r[5] or [],
             "preconditions": r[6], "steps": r[7] or [], "expected_behaviour": r[8],
             "source_chunk_ids": [int(x) for x in (r[9] or []) if str(x).isdigit()],
             "case_type": r[10], "folder_id": r[11]} for r in cur.fetchall()]


def _with_ancestors(cur, owner_id: int, project: str, fids: set[int]) -> set[int]:
    """Folders that lost cases plus their ancestors — the only ones a run may prune, so a
    user's own untouched empty folder is never deleted."""
    cur.execute("select id, parent_id from use_case_folder where owner_id=%s and project=%s", (owner_id, project))
    parent = {r[0]: r[1] for r in cur.fetchall()}
    out: set[int] = set()
    for fid in fids:
        while fid is not None and fid not in out:
            out.add(fid)
            fid = parent.get(fid)
    return out


def _subtheme_rule(categories: list[str] | None) -> str:
    if categories:
        return ("assign this use case to EXACTLY ONE of these categories, copied verbatim: "
                + "; ".join(categories) + f'. If none fits, use "{_OTHER}". Never leave it blank.')
    return _SUBTHEME_FREE


def _scope_categories(owner_id: int, project: str, scope: str, scope_reqs: list[dict],
                      route: GenRoute | None, memo: dict) -> list[str] | None:
    """The sub-folders a scope already has keep new cases in the same folders; otherwise a
    large scope derives a fixed taxonomy (pass 1) and a small one free-groups."""
    if scope in memo:
        return memo[scope]
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select c.name from use_case_folder c join use_case_folder s on s.id = c.parent_id "
                    "where s.owner_id=%s and s.project=%s and s.parent_id is null and lower(s.name)=lower(%s) "
                    "order by c.ordinal, c.id", (owner_id, project, scope))
        cats = [r[0] for r in cur.fetchall()] or None
    if cats is None and len(scope_reqs) > _SCOPE_BATCH:
        try:
            cats = _derive_categories(owner_id, project, scope, scope_reqs, route) or None
        except Exception as e:  # noqa: BLE001 — fall back to free grouping
            log.warning("taxonomy failed (scope %r): %s", scope, e)
    memo[scope] = cats
    return cats


def _update_unit(owner_id: int, project: str, u: dict, removed_ids: set[int], route: GenRoute | None,
                 categories: list[str] | None, coverage: str) -> tuple[dict, list[dict]]:
    """One LLM call for a unit that has existing cases → ({card index: (action, cleaned case)},
    cleaned new cases). Cached by the unit's exact input. An 'update' that fails validation
    degrades to 'keep' (never lose a case to a malformed answer); unparseable output raises
    so the unit stays pending."""
    r = route or GenRoute()
    removed = sorted(c for c in removed_ids if any(c in card["source_chunk_ids"] for card in u["cards"]))
    key = ["upd", owner_id, project, inc.cache_key(u["scope"], u["items"], u["context"], removed, u["cards"]),
           r.model or settings.gen_model or "", categories or "free", coverage]
    d = cache.get(_CACHE_NS, key)
    if not d:
        prompt = (inc.UPDATE_PROMPT.replace("{scope}", u["scope"])
                  .replace("{subtheme_rule}", _subtheme_rule(categories))
                  .replace("{coverage_rule}", _COVERAGE_FULL if coverage == "full" else _COVERAGE_FUNCTIONAL)
                  .replace("{requirements}", _candidate_lines(u["items"]) or "(none)")
                  .replace("{context}", _candidate_lines(u["context"]) or "(none)")
                  .replace("{removed}", ", ".join(str(c) for c in removed) or "(none)")
                  .replace("{cases}", inc.case_lines(u["cards"])))
        text = engine.complete_chat([{"role": "user", "content": prompt}],
                                    provider=r.provider, api_key=r.api_key, base_url=r.base_url,
                                    model=r.model, temperature=0.0, max_tokens=_GEN_MAX_TOKENS)
        d = _parse_json(text)
        if not d:
            raise ValueError("unparseable update response (possible truncation)")
        cache.set(_CACHE_NS, key, d, owner_id=owner_id, project=project)
    allowed = {it["chunk_id"] for it in u["items"]} | {it["chunk_id"] for it in u["context"]}
    decided, new_raw = inc.parse_update(d, len(u["cards"]))
    out: dict[int, tuple[str, dict | None]] = {}
    for i, (action, raw) in decided.items():
        if action == "update":
            cleaned = _clean_use_cases({"use_cases": [raw]}, allowed, categories)
            out[i] = ("update", cleaned[0]) if cleaned else ("keep", None)
        else:
            out[i] = (action, None)
    return out, _clean_use_cases({"use_cases": new_raw}, allowed, categories)


def _remove_card(cur, owner_id: int, card: dict, changed_by: int) -> None:
    ucv.record_change(cur, card["id"], "regenerate", owner_id, changed_by)   # history first → Trash
    cur.execute("delete from use_case where id=%s and owner_id=%s", (card["id"], owner_id))


def _apply_unit(owner_id: int, project: str, u: dict, decided: dict, new_cards: list[dict],
                removed_ids: set[int], uc_start: int, seen: set[str], changed_by: int) -> tuple[dict, set[int]]:
    """Apply one unit ATOMICALLY: removals (history kept → Trash), in-place updates (same uid
    and code → a version diff shows 'modified'), citations of removed requirements dropped from
    kept cases, new cases inserted, and the unit's requirements written to the basis — one
    transaction, so an interruption leaves the unit pending, never half-applied."""
    stats = {"added": 0, "updated": 0, "removed": 0, "kept": 0}
    emptied: set[int] = set()
    with pool().connection() as conn, conn.cursor() as cur:
        for i, card in enumerate(u["cards"]):
            action, uc = decided.get(i, ("keep", None))
            if action == "remove":
                _remove_card(cur, owner_id, card, changed_by)
                if card["folder_id"] is not None:
                    emptied.add(card["folder_id"])
                stats["removed"] += 1
            elif action == "update":
                cur.execute(
                    "update use_case set title=%s, description=%s, roles=%s, preconditions=%s, steps=%s, "
                    "expected_behaviour=%s, source_chunk_ids=%s, case_type=%s, updated_at=now() "
                    "where id=%s and owner_id=%s",
                    (uc["title"], uc["description"], Jsonb(uc["roles"]), uc["preconditions"], Jsonb(uc["steps"]),
                     uc["expected_behaviour"], Jsonb(uc["source_chunk_ids"]), uc.get("case_type", _CASE_DEFAULT),
                     card["id"], owner_id))
                ucv.record_change(cur, card["id"], "regenerate", owner_id, changed_by)
                seen.add(uc["title"].lower())
                stats["updated"] += 1
            else:
                live = [c for c in card["source_chunk_ids"] if c not in removed_ids]
                if live != card["source_chunk_ids"]:
                    cur.execute("update use_case set source_chunk_ids=%s, updated_at=now() where id=%s and owner_id=%s",
                                (Jsonb(live), card["id"], owner_id))
                    ucv.record_change(cur, card["id"], "regenerate", owner_id, changed_by)
                stats["kept"] += 1
        fresh = []
        for uc in new_cards:
            if uc["title"].lower() not in seen:
                seen.add(uc["title"].lower())
                fresh.append(uc)
        stats["added"] = _insert_cards(cur, owner_id, project, u["scope"], fresh, uc_start, changed_by)
        _set_basis(cur, owner_id, project, u["items"])
        conn.commit()
    return stats, emptied


def _prune_empty_folders(owner_id: int, project: str, candidates: set[int] | None = None) -> None:
    """Delete folders left with no use cases and no child folders — repeatedly, so a parent
    emptied by pruning its children is removed too. With `candidates`, only those folder ids
    are eligible (a run prunes ONLY the folders it actually emptied, never a user's own
    untouched empty folder). candidates=None prunes every empty folder."""
    if candidates is not None and not candidates:
        return
    scope = "and f.id = any(%s) " if candidates is not None else ""
    args = (owner_id, project) + ((list(candidates),) if candidates is not None else ())
    with pool().connection() as conn, conn.cursor() as cur:
        while True:
            cur.execute(
                "delete from use_case_folder f where f.owner_id=%s and f.project=%s " + scope +
                "and not exists (select 1 from use_case uc where uc.folder_id = f.id) "
                "and not exists (select 1 from use_case_folder c where c.parent_id = f.id)",
                args,
            )
            if cur.rowcount == 0:
                break
        conn.commit()


def _state(owner_id: int, project: str, reqs: list[dict]) -> tuple[dict, list[dict], dict[str, set[str]]]:
    _ensure_basis(owner_id, project, reqs)
    with pool().connection() as conn, conn.cursor() as cur:
        basis = _basis(cur, owner_id, project)
        cards = _cards(cur, owner_id, project)
        seen = _existing_titles(cur, owner_id, project)
    return inc.plan(reqs, basis, cards), cards, seen


def resume_state(owner_id: int, project: str) -> dict:
    """What an Update would do — derived from the requirement basis, so it survives restarts:
    requirements added / changed / removed since the use cases were generated (`superseded` =
    all of them, `stale` when cases exist), and `pending` work units left."""
    reqs = list_requirements(owner_id, project)
    if not reqs:
        return {"exists": False, "total": 0, "done": 0, "failed": 0, "pending": 0, "complete": False,
                "resumable": False, "superseded": 0, "stale": False, "changed": 0, "added": 0, "removed": 0}
    p, _, _ = _state(owner_id, project, reqs)
    work = inc.units(p, reqs, _SCOPE_BATCH) if (p["pending_ids"] or p["removed_ids"]) else []
    exists = has_use_cases(owner_id, project)
    todo = len(p["pending_ids"]) + len(p["removed_ids"])
    total = len(_batches(reqs))
    return {"exists": exists, "total": total, "done": max(0, total - len(work)), "failed": 0,
            "pending": len(work), "complete": not work, "resumable": False,
            "superseded": todo, "stale": exists and todo > 0,
            "changed": len(p["changed_ids"]), "added": len(p["new_ids"]), "removed": len(p["removed_ids"])}


def generate_for_project(owner_id: int, project: str, *, route: GenRoute | None = None,
                         replace: bool = False, mode: str | None = None,
                         coverage: str = _CASE_DEFAULT, on_progress=None) -> dict:
    """Generate / update the use-case tree of a BRD INCREMENTALLY, at requirement level.

    One engine for every mode: it processes only the requirements whose current text is not
    in the requirement basis (new or changed) plus those removed from the BRD —
      * a case citing ONLY removed requirements is removed (history kept → Trash), no LLM;
      * a unit of pending requirements with no existing cases is plain generation;
      * a unit with existing cases asks the model keep / update / remove per case (+ new ones):
        updates happen IN PLACE, so a case keeps its uid and code across versions.
    Nothing else is touched — unaffected cases and manual edits stay exactly as they are.
    The live cases are the suite's DRAFT: the user reviews the resulting diff and publishes it
    as a new version (published versions, and the results recorded on them, never change).

    `mode`: "resume" / "sync" run the engine; "replace" first clears the tree (and the basis)
    to regenerate everything — every case then shows as removed + added in the next version.
    DURABLE by construction: each unit commits atomically with its basis rows; a failed or
    interrupted unit stays pending and the next run continues (no ledger, nothing duplicated).
    `route` is the user's BYOK provider + model (None → system model); the model id is part of
    every cache key."""
    reqs = list_requirements(owner_id, project)
    if not reqs:
        return {"error": "This BRD has no requirements to derive use cases from."}
    mode = mode or ("replace" if replace else "resume")
    coverage = "full" if (coverage or "").lower() == "full" else _CASE_DEFAULT
    if mode == "replace":
        clear(owner_id, project)
    p, _, seen_titles = _state(owner_id, project, reqs)
    stats = {"added": 0, "updated": 0, "removed": 0, "kept": 0}
    emptied: set[int] = set()
    if p["orphans"] or p["removed_ids"]:
        with pool().connection() as conn, conn.cursor() as cur:
            for card in p["orphans"]:                  # every requirement it cites is gone
                _remove_card(cur, owner_id, card, owner_id)
                if card["folder_id"] is not None:
                    emptied.add(card["folder_id"])
            cur.execute("delete from use_case_req_basis where owner_id=%s and project=%s and chunk_id = any(%s)",
                        (owner_id, project, list(p["removed_ids"])))
            conn.commit()
        stats["removed"] += len(p["orphans"])
    work = inc.units(p, reqs, _SCOPE_BATCH) if (p["pending_ids"] or p["affected"]) else []
    groups = dict(_group_by_scope(reqs))
    total = len(work)
    uc_counter = _next_uc_number(owner_id, project)
    memo: dict[str, list[str] | None] = {}
    errors: list[dict] = []
    for i, u in enumerate(work):
        scope = u["scope"]
        if on_progress:
            on_progress(i, total, scope)
        cats = _scope_categories(owner_id, project, scope, groups.get(scope, []), route, memo)
        try:
            if u["cards"]:
                decided, new = _update_unit(owner_id, project, u, p["removed_ids"], route, cats, coverage)
            else:
                decided, new = {}, _generate_batch(owner_id, project, scope, u["items"], route, cats, coverage)
                if not new:                            # empty parse / truncated JSON → stays pending
                    raise ValueError("no valid use cases parsed (possible truncation)")
            s, em = _apply_unit(owner_id, project, u, decided, new, p["removed_ids"], uc_counter,
                                seen_titles.setdefault(scope, set()), owner_id)
        except Exception as e:  # noqa: BLE001 — a failed unit must not lose the rest
            log.warning("use-case unit failed (scope %r): %s", scope, e)
            errors.append({"scope": scope, "error": str(e)})
            continue
        uc_counter += s["added"]
        emptied |= em
        for k in stats:
            stats[k] += s[k]
    if emptied:
        with pool().connection() as conn, conn.cursor() as cur:
            candidates = _with_ancestors(cur, owner_id, project, emptied)
        _prune_empty_folders(owner_id, project, candidates)
    if on_progress:
        on_progress(total, total, None)
    return {"scopes": len({u["scope"] for u in work}), "batches": total, "use_cases": stats["added"],
            "updated": stats["updated"], "removed": stats["removed"], "skipped": 0, "mode": mode,
            "errors": errors}


# --- restore a published version into the draft ----------------------------

def restore_version(owner_id: int, project: str, version_id: int, by: int | None = None) -> dict:
    """Make the DRAFT equal to a published version — git-revert style: nothing published is
    rewritten; the user reviews the result and publishes it as a NEW version. Cases not in the
    version are removed (history kept → Trash), the version's cases are written back IN PLACE
    under their own uid and code (a deleted one is re-inserted, its test state re-derived),
    folders and order follow the version. Doubles as "discard unpublished changes" (restore
    the latest version).

    A requirement the restored cases cited at a text that has changed since is dropped from the
    requirement basis, so Update then offers to bring exactly those cases up to date — instead
    of old cases silently sitting on new requirements."""
    from backend.use_cases import suites          # lazy: suites ↔ generation stay import-independent
    by = by if by is not None else owner_id
    removed = updated = reinserted = 0
    emptied: set[int] = set()
    with pool().connection() as conn, conn.cursor() as cur:
        suites._lock(cur, owner_id, project)
        suites._own_version(cur, owner_id, project, version_id)
        cur.execute("select number from use_case_suite_version where id = %s", (version_id,))
        number = cur.fetchone()[0]
        target = suites._version_rows(cur, version_id)
        draft, _ = suites._draft_rows(cur, owner_id, project)
        cur.execute("select uid, id, folder_id from use_case where owner_id = %s and project = %s", (owner_id, project))
        live = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        for uid, (pk, fid) in live.items():
            if uid not in target:
                ucv.record_change(cur, pk, "delete", owner_id, by)          # history first → Trash
                cur.execute("delete from use_case where id = %s and owner_id = %s", (pk, owner_id))
                if fid is not None:
                    emptied.add(fid)
                removed += 1
        for uid, row in target.items():
            cur_row = draft.get(uid)
            # the case itself (not its cited requirements' text — a restore can't change that)
            if cur_row and (cur_row["content_hash"], cur_row["folder_path"], cur_row["ordinal"]) == \
                    (row["content_hash"], row["folder_path"], row["ordinal"]):
                continue                                                  # already identical
            d = row["data"]
            fid, parent = None, None
            for name in row["folder_path"] or []:
                fid = parent = _folder(cur, owner_id, project, parent, name)
            vals = (fid, d.get("uc_id"), d.get("title") or "", d.get("description") or "", Jsonb(d.get("roles") or []),
                    d.get("preconditions") or "", Jsonb(d.get("steps") or []), d.get("expected_behaviour") or "",
                    Jsonb(d.get("source_chunk_ids") or []), d.get("case_type") or _CASE_DEFAULT, row["ordinal"])
            if uid in live:
                pk, old_fid = live[uid]
                cur.execute("update use_case set folder_id=%s, uc_id=%s, title=%s, description=%s, roles=%s, "
                            "preconditions=%s, steps=%s, expected_behaviour=%s, source_chunk_ids=%s, case_type=%s, "
                            "ordinal=%s, updated_at=now() where id=%s and owner_id=%s", vals + (pk, owner_id))
                if old_fid is not None and old_fid != fid:
                    emptied.add(old_fid)
                updated += 1
            else:
                cur.execute("insert into use_case (folder_id, uc_id, title, description, roles, preconditions, steps, "
                            "expected_behaviour, source_chunk_ids, case_type, ordinal, uid, owner_id, project) "
                            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
                            vals + (uid, owner_id, project))
                pk = cur.fetchone()[0]
                uct.resync_card(cur, uid, owner_id)                         # its test state lives in the timeline
                reinserted += 1
            ucv.record_change(cur, pk, "restore", owner_id, by)
        # requirements the restored definitions cited at an older text → pending again (Update)
        cited = {int(c): h for row in target.values() for c, h in (row["req_hashes"] or {}).items() if h}
        stale: list[int] = []
        if cited:
            cur.execute("select id, md5(text) from brd_chunk where id = any(%s)", (list(cited),))
            now = dict(cur.fetchall())
            stale = [c for c, h in cited.items() if now.get(c) != h]
            if stale:
                # the basis goes back to the text these definitions were generated from, so the
                # engine sees exactly "requirement changed since" and updates the cases citing it
                cur.executemany(
                    "insert into use_case_req_basis (owner_id, project, chunk_id, hash) values (%s,%s,%s,%s) "
                    "on conflict (owner_id, project, chunk_id) do update set hash = excluded.hash, updated_at = now()",
                    [(owner_id, project, c, cited[c]) for c in stale if c in now])
        candidates = _with_ancestors(cur, owner_id, project, emptied) if emptied else set()
        conn.commit()
    if candidates:
        _prune_empty_folders(owner_id, project, candidates)
    return {"version": number, "removed": removed, "updated": updated, "restored": reinserted,
            "requirements_changed_since": len(stale)}


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
            "select uc.id, uc.folder_id, uc.uc_id, uc.title, uc.description, uc.roles, uc.preconditions, "
            "uc.steps, uc.expected_behaviour, uc.source_chunk_ids, uc.ordinal, uc.status, uc.uid, "
            "uc.case_type, uc.test_status, uc.in_scope, "
            # needs_retest (derived): the verdict's pinned test-definition hash has moved on
            "(uc.test_status in ('blocked','failed','passed') and uc.tested_hash is not null "
            " and uc.tested_hash <> uc.test_basis), "
            # last audit entry: who last touched this case, when, and what they did
            "la.actor_email, la.created_at, la.summary "
            "from use_case uc "
            "left join lateral (select actor_email, created_at, summary from use_case_audit a "
            "  where a.use_case_uid = uc.uid and a.owner_id = uc.owner_id "
            "  order by a.created_at desc, a.id desc limit 1) la on true "
            "where uc.owner_id = %s and uc.project = %s order by uc.folder_id, uc.ordinal, uc.id",
            (owner_id, project),
        )
        ucs = [{"id": r[0], "folder_id": r[1], "uc_id": r[2], "title": r[3], "description": r[4],
                "roles": r[5], "preconditions": r[6], "steps": r[7], "expected_behaviour": r[8],
                "source_chunk_ids": r[9], "ordinal": r[10], "status": r[11], "uid": r[12],
                "case_type": r[13], "test_status": r[14], "in_scope": r[15], "needs_retest": bool(r[16]),
                "last_by": r[17], "last_at": r[18].isoformat() if r[18] else None, "last_summary": r[19]}
               for r in cur.fetchall()]
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


def update_use_case(owner_id: int, uc_pk: int, fields: dict, changed_by: int | None = None) -> bool:
    """Edit whitelisted fields of one use case (owner-scoped), attributed to `changed_by`."""
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
    if "case_type" in fields:                                   # constrained enum, not free text
        ct = _norm(str(fields["case_type"] or "")).lower()
        if ct in _CASE_TYPES:
            sets.append("case_type = %s")
            vals.append(ct)
    if not sets:
        return False
    sets.append("updated_at = now()")
    vals += [uc_pk, owner_id]
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(f"update use_case set {', '.join(sets)} where id = %s and owner_id = %s", vals)
        ok = cur.rowcount > 0
        if ok:
            ucv.record_change(cur, uc_pk, "edit", owner_id, changed_by)   # snapshot new state (audit + backup)
        conn.commit()
    return ok


def delete_use_case(owner_id: int, uc_pk: int, changed_by: int | None = None) -> bool:
    with pool().connection() as conn, conn.cursor() as cur:
        ucv.record_change(cur, uc_pk, "delete", owner_id, changed_by)     # backup before removal (recoverable in Trash)
        cur.execute("delete from use_case where id = %s and owner_id = %s", (uc_pk, owner_id))
        ok = cur.rowcount > 0
        conn.commit()
    return ok


def move_use_case(owner_id: int, uc_pk: int, folder_id: int, changed_by: int | None = None) -> bool:
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
            ucv.record_change(cur, uc_pk, "move", owner_id, changed_by)   # audit the folder move
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


def delete_folder(owner_id: int, folder_id: int, changed_by: int | None = None) -> bool:
    """Delete a folder and everything under it (subfolders + use cases cascade).
    Records a 'delete' version for every use case in the folder subtree FIRST, in the
    same transaction, so folder deletion honours the invariant — its cards stay
    recoverable from Trash instead of being lost by the raw cascade."""
    changed_by = changed_by if changed_by is not None else owner_id
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """with recursive sub as (
                   select id from use_case_folder where id = %s and owner_id = %s
                   union all
                   select f.id from use_case_folder f join sub on f.parent_id = sub.id
               )
               select uc.id from use_case uc where uc.folder_id in (select id from sub)""",
            (folder_id, owner_id),
        )
        for (uc_pk,) in cur.fetchall():
            ucv.record_change(cur, uc_pk, "delete", owner_id, changed_by)   # backup before cascade
        cur.execute("delete from use_case_folder where id = %s and owner_id = %s", (folder_id, owner_id))
        ok = cur.rowcount > 0
        conn.commit()
    return ok

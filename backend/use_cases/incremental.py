"""Incremental use-case generation — the pure half (no DB, no LLM).

The requirement BASIS (use_case_req_basis) records the text hash of every requirement the use
cases were last generated from. Against the live requirements that gives, per requirement:

    new       live, never generated from           → generate cases for it
    changed   live, text differs from the basis    → update the cases citing it
    removed   in the basis (or cited) but gone     → drop it from the cases citing it

and per use case: an ORPHAN (every requirement it cites is gone → removed, no LLM needed) or
AFFECTED (cites a new/changed/removed requirement → the LLM decides keep / update / remove).
Everything else is untouched — including manual edits.

`units` turns that into bounded LLM calls per scope. A unit with no existing cases is plain
generation; one with cases uses the UPDATE prompt, which answers per existing case by ref, so an
updated case keeps its uid and code (a version diff shows "modified", not "removed + added").
"""
from __future__ import annotations

import hashlib
import json
import re


def req_hash(text: str | None) -> str:
    """md5 hex of the requirement text — equals Postgres md5(text), used by suite versions."""
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


def _scope(req: dict) -> str:
    return re.sub(r"\s+", " ", req.get("section") or "").strip() or "General"


def plan(reqs: list[dict], basis: dict[int, str], cards: list[dict]) -> dict:
    """reqs: live requirements in document order ({chunk_id, text, section, …});
    basis: {chunk_id: hash}; cards: live use cases ({id, source_chunk_ids, …})."""
    live = {r["chunk_id"]: req_hash(r["text"]) for r in reqs}
    pending = [r for r in reqs if basis.get(r["chunk_id"]) != live[r["chunk_id"]]]
    pending_ids = {r["chunk_id"] for r in pending}
    changed_ids = {c for c in pending_ids if c in basis}
    cited = {int(c) for card in cards for c in card["source_chunk_ids"]}
    removed_ids = (set(basis) | cited) - set(live)
    orphans, affected = [], []
    for card in cards:
        cids = {int(c) for c in card["source_chunk_ids"]}
        if not cids:
            continue                                  # a manual case with no citations: never touched
        if cids <= removed_ids:
            orphans.append(card)
        elif cids & (pending_ids | removed_ids):
            affected.append(card)
    return {"pending": pending, "pending_ids": pending_ids, "changed_ids": changed_ids,
            "new_ids": pending_ids - changed_ids, "removed_ids": removed_ids,
            "orphans": orphans, "affected": affected}


def units(p: dict, reqs: list[dict], batch_size: int) -> list[dict]:
    """Bounded work units, per scope in document order: up to `batch_size` pending requirements
    each, plus the affected cases (a case joins the unit holding the first pending requirement it
    cites, else its scope's first unit) and, as context, the live requirements those cases also
    cite. A scope with affected cases but nothing pending (only removals) still gets one unit."""
    by_id = {r["chunk_id"]: r for r in reqs}
    order = {r["chunk_id"]: i for i, r in enumerate(reqs)}
    scopes: list[str] = []
    pend: dict[str, list[dict]] = {}
    for r in p["pending"]:
        s = _scope(r)
        if s not in pend:
            pend[s] = []
            scopes.append(s)
        pend[s].append(r)
    card_scope: dict[int, str] = {}
    for card in p["affected"]:
        live = sorted((int(c) for c in card["source_chunk_ids"] if int(c) in by_id), key=order.get)
        s = _scope(by_id[live[0]])
        card_scope[card["id"]] = s
        if s not in pend:
            pend[s] = []
            scopes.append(s)
    out: list[dict] = []
    for s in scopes:
        items = pend[s]
        chunks = [items[i:i + batch_size] for i in range(0, len(items), batch_size)] or [[]]
        us = [{"scope": s, "items": c, "cards": [], "context": []} for c in chunks]
        for card in p["affected"]:
            if card_scope[card["id"]] != s:
                continue
            cids = {int(c) for c in card["source_chunk_ids"]}
            home = next((u for u in us if cids & {r["chunk_id"] for r in u["items"]}), us[0])
            home["cards"].append(card)
        for u in us:
            have = {r["chunk_id"] for r in u["items"]}
            ctx = {int(c) for card in u["cards"] for c in card["source_chunk_ids"]} - have
            u["context"] = sorted((by_id[c] for c in ctx if c in by_id), key=lambda r: order[r["chunk_id"]])
        out.extend(us)
    return out


UPDATE_PROMPT = """You are a QA analyst maintaining USE CASES derived from a Business Requirements Document
(scope "{scope}"). Some requirements CHANGED or were ADDED, and some requirements the cases cite were
REMOVED. Below are the current requirements and the EXISTING use cases that depend on them.

For EVERY existing use case (by its ref) decide exactly one action:
- "keep": it is still correct against the current requirements, as written
- "update": the change alters it — return the FULL corrected use case (keep its title unless its meaning changed)
- "remove": no current requirement supports it any more
Then add NEW use cases only for behaviour the requirements now require that no kept or updated case covers.
Never invent behaviour the requirements do not support.

{coverage_rule}

Every returned use case (updated or new) is complete, with: case_type ("functional" | "negative" | "edge"),
title, description, roles (array, at least one), preconditions, steps (array, at least one),
expected_behaviour (non-empty), source_chunk_ids (integers from the requirement lists below only, at least
one) and subtheme: {subtheme_rule}

CHANGED OR NEW REQUIREMENTS:
{requirements}

UNCHANGED REQUIREMENTS THE EXISTING CASES ALSO CITE (context only):
{context}

REMOVED REQUIREMENTS (no longer in the BRD — cite them no more): {removed}

EXISTING USE CASES:
{cases}

Return ONLY JSON (no prose, no code fences):
{"existing": [{"ref": "E1", "action": "keep"},
              {"ref": "E2", "action": "update", "use_case": {"case_type": "functional", "subtheme": "...",
               "title": "...", "description": "...", "roles": ["..."], "preconditions": "...",
               "steps": ["..."], "expected_behaviour": "...", "source_chunk_ids": [123]}},
              {"ref": "E3", "action": "remove"}],
 "new": [{"case_type": "...", "subtheme": "...", "title": "...", "description": "...", "roles": ["..."],
          "preconditions": "...", "steps": ["..."], "expected_behaviour": "...", "source_chunk_ids": [123]}]}
"""


def case_lines(cards: list[dict]) -> str:
    """The existing cases for the update prompt, addressed by short refs E1, E2, …"""
    out = []
    for i, c in enumerate(cards, 1):
        steps = " ".join(f"{n}) {s}" for n, s in enumerate(c.get("steps") or [], 1))
        out.append(f"E{i} [{c.get('case_type') or 'functional'}] {c.get('title')}\n"
                   f"  preconditions: {c.get('preconditions') or '-'}\n"
                   f"  steps: {steps or '-'}\n"
                   f"  expected: {c.get('expected_behaviour') or '-'}\n"
                   f"  cites: {', '.join(str(x) for x in c.get('source_chunk_ids') or [])}")
    return "\n".join(out)


def parse_update(d: dict, n_cards: int) -> tuple[dict[int, tuple[str, dict | None]], list]:
    """{card index: (action, raw use case | None)} + the raw new cases. A card the model did not
    mention — or answered with an unknown action — is KEPT (never delete on a missing answer)."""
    decided: dict[int, tuple[str, dict | None]] = {}
    for e in d.get("existing") if isinstance(d.get("existing"), list) else []:
        if not isinstance(e, dict):
            continue
        m = re.fullmatch(r"E(\d+)", str(e.get("ref") or "").strip(), re.I)
        if not m or not (1 <= int(m.group(1)) <= n_cards) or int(m.group(1)) - 1 in decided:
            continue
        action = str(e.get("action") or "").strip().lower()
        uc = e.get("use_case") if isinstance(e.get("use_case"), dict) else None
        if action == "update" and uc is None:
            action = "keep"
        decided[int(m.group(1)) - 1] = (action if action in ("keep", "update", "remove") else "keep", uc)
    for i in range(n_cards):
        decided.setdefault(i, ("keep", None))
    new = d.get("new") if isinstance(d.get("new"), list) else []
    return decided, new


def cache_key(scope: str, items: list[dict], context: list[dict], removed: list[int], cards: list[dict]) -> str:
    """Deterministic identity of an update unit's LLM input (for the answer cache)."""
    blob = json.dumps([scope,
                       [(r["chunk_id"], req_hash(r["text"])) for r in items],
                       [(r["chunk_id"], req_hash(r["text"])) for r in context], sorted(removed),
                       [(c["uid"], c.get("title"), c.get("preconditions"), c.get("steps"),
                         c.get("expected_behaviour"), c.get("source_chunk_ids"), c.get("case_type")) for c in cards]],
                      ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

"""In-place requirement (chunk) editing — QA "change a requirement".

Phase 1: edit one requirement's text → re-embed → re-index → cache-bust → audit.
Phase 2: structural edits — an over-long edit SPLITS into several chunks, and
requirements can be ADDED or REMOVED. Inserting shifts ordinals, so the
(document_id, ordinal) uniqueness is DEFERRABLE (migration 015) and we defer it
for the shift; uniqueness is validated at commit.

Invariants every mutating op keeps:
  * embed OUTSIDE the write tx (slow network call — never hold a row lock over it),
  * chunk text + embedding written in ONE tx (they never disagree),
  * `search_tsv` (generated) recomputes automatically; HNSW updates on embedding write,
  * the project's retrieve+answer caches are busted (answer cache is keyed on chunk
    ids, and a chunk's text can change under a stable id, so it MUST be busted),
  * an audit row records the change (kind edit|add|delete, old → new, who, when).
"""
from __future__ import annotations

import json

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from .. import cache
from ..config import settings
from ..db import pool
from ..versioning import ensure_baseline_tx
from .chunker import _est_tokens, _hash, _split
from .embedder import embed_documents
from .store import _vec_literal

MAX_BATCH_OPS = 50          # ceiling on operations in one atomic change set (cost + DoS guard)


class ChunkNotFound(Exception):
    """No such chunk/BRD for this owner (missing, or not owned by the user)."""


class StaleRequirement(Exception):
    """Optimistic-concurrency conflict: a requirement changed between the moment a
    change was planned and the moment it was applied, so applying the planned edit
    would silently clobber the newer text. The caller should re-plan against fresh
    text. Carries the offending chunk_id."""
    def __init__(self, chunk_id: int):
        super().__init__(f"requirement {chunk_id} changed since it was planned")
        self.chunk_id = chunk_id


def _defer_ordinals(cur) -> None:
    """Defer the (document_id, ordinal) uniqueness so we can shift within the tx."""
    cur.execute("set constraints brd_chunk_doc_ordinal_uk deferred")


def _lock_doc(cur, document_id: int) -> None:
    """Serialize all writers to one BRD for the rest of this transaction. Ordinal
    shifts + inserts across a document are not safe to interleave (the deferred
    uniqueness check would fail one writer at commit); an xact-scoped advisory lock
    keyed on the document makes concurrent edits to the SAME BRD queue, while edits
    to other BRDs stay fully parallel. Released automatically at commit/rollback."""
    cur.execute("select pg_advisory_xact_lock(%s)", (document_id,))


def _insert_chunk(cur, document_id: int, req_id, section, ordinal: int,
                  text: str, vec_lit: str) -> int:
    cur.execute(
        """insert into brd_chunk (document_id, req_id, section, ordinal, text, token_count, content_hash)
           values (%s, %s, %s, %s, %s, %s, %s) returning id""",
        (document_id, req_id, section, ordinal, text, _est_tokens(text), _hash(text)),
    )
    cid = cur.fetchone()[0]
    cur.execute(
        "insert into brd_embedding (chunk_id, model, embedding) values (%s, %s, %s::vector)",
        (cid, settings.embed_model_docs, vec_lit),
    )
    return cid


def _audit(cur, document_id: int, chunk_id, req_id, old_text, new_text, kind, changed_by,
           change_id: int | None = None) -> None:
    cur.execute(
        """insert into requirement_change
             (document_id, chunk_id, req_id, old_text, new_text, kind, changed_by, change_id)
           values (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (document_id, chunk_id, req_id, old_text, new_text, kind, changed_by, change_id),
    )
    cur.execute("update brd_document set updated_at = now() where id = %s", (document_id,))


# --- cursor-level ops (run inside a locked, ordinal-deferred, pre-embedded tx) ---
# Each returns quickly and raises (ChunkNotFound / StaleRequirement) to abort the
# whole transaction; `pieces`/`vectors` are pre-computed so no embedding happens
# under the lock. Optimistic concurrency compares the CURRENT text's hash to the
# hash the change was planned against (`base_hash`) — independent of how the stored
# content_hash column is derived, so it can't false-conflict on a normalization diff.

def _op_edit(cur, document_id: int, owner_id: int, chunk_id: int, new_text: str,
             pieces: list[str], vectors: list, base_hash: str | None,
             changed_by: int | None, kind: str = "edit", change_id: int | None = None) -> tuple:
    """Edit one requirement in place (splitting if over the ceiling). Returns
    (req_id, changed: bool)."""
    cur.execute(
        """select c.text, c.req_id, c.section, c.ordinal
           from brd_chunk c join brd_document d on d.id = c.document_id
           where c.id = %s and d.owner_id = %s and c.document_id = %s""",
        (chunk_id, owner_id, document_id),
    )
    row = cur.fetchone()
    if not row:
        raise ChunkNotFound(chunk_id)
    old_text, req_id, section, ordinal = row
    if base_hash is not None and _hash(old_text or "") != base_hash:
        raise StaleRequirement(chunk_id)
    if _hash(old_text or "") == _hash(new_text):
        return req_id, False
    if len(pieces) > 1:
        cur.execute("update brd_chunk set ordinal = ordinal + %s where document_id = %s and ordinal > %s",
                    (len(pieces) - 1, document_id, ordinal))
    cur.execute("update brd_chunk set text = %s, token_count = %s, content_hash = %s where id = %s",
                (pieces[0], _est_tokens(pieces[0]), _hash(pieces[0]), chunk_id))
    cur.execute(
        """insert into brd_embedding (chunk_id, model, embedding) values (%s, %s, %s::vector)
           on conflict (chunk_id) do update set model = excluded.model, embedding = excluded.embedding""",
        (chunk_id, settings.embed_model_docs, _vec_literal(vectors[0])),
    )
    for i, piece in enumerate(pieces[1:], start=1):
        _insert_chunk(cur, document_id, req_id, section, ordinal + i, piece, _vec_literal(vectors[i]))
    _audit(cur, document_id, chunk_id, req_id, old_text, new_text, kind, changed_by, change_id)
    return req_id, True


def _op_add(cur, document_id: int, text: str, pieces: list[str], vectors: list,
            req_id: str | None, section: str | None, after_chunk_id: int | None,
            changed_by: int | None, change_id: int | None = None) -> int:
    """Insert a new requirement after a chunk (or appended). Ordinals are resolved
    live under the lock, so a batch's earlier shifts are already reflected. Returns
    the first inserted chunk_id."""
    if after_chunk_id is not None:
        cur.execute("select ordinal from brd_chunk where id = %s and document_id = %s",
                    (after_chunk_id, document_id))
        r = cur.fetchone()
        if not r:
            raise ChunkNotFound(after_chunk_id)
        insert_at = r[0] + 1
    else:
        cur.execute("select coalesce(max(ordinal), -1) from brd_chunk where document_id = %s", (document_id,))
        insert_at = cur.fetchone()[0] + 1
    cur.execute("select coalesce(max(ordinal), -1) from brd_chunk where document_id = %s", (document_id,))
    if insert_at <= cur.fetchone()[0]:          # making room only when inserting before the end
        cur.execute("update brd_chunk set ordinal = ordinal + %s where document_id = %s and ordinal >= %s",
                    (len(pieces), document_id, insert_at))
    first_id = None
    for i, piece in enumerate(pieces):
        cid = _insert_chunk(cur, document_id, req_id, section, insert_at + i, piece, _vec_literal(vectors[i]))
        first_id = first_id or cid
    _audit(cur, document_id, first_id, req_id, None, text, "add", changed_by, change_id)
    return first_id


def _op_delete(cur, document_id: int, owner_id: int, chunk_id: int,
               base_hash: str | None, changed_by: int | None, change_id: int | None = None) -> str | None:
    """Delete one requirement (embedding cascades; ordinal gaps are fine). Returns
    its req_id."""
    cur.execute(
        """select c.text, c.req_id from brd_chunk c join brd_document d on d.id = c.document_id
           where c.id = %s and d.owner_id = %s and c.document_id = %s""",
        (chunk_id, owner_id, document_id),
    )
    row = cur.fetchone()
    if not row:
        raise ChunkNotFound(chunk_id)
    old_text, req_id = row
    if base_hash is not None and _hash(old_text or "") != base_hash:
        raise StaleRequirement(chunk_id)
    _audit(cur, document_id, chunk_id, req_id, old_text, "", "delete", changed_by, change_id)  # audit before delete (FK SET NULL)
    cur.execute("delete from brd_chunk where id = %s", (chunk_id,))                   # cascades brd_embedding
    return req_id


# --- reads ------------------------------------------------------------------

def list_requirements(owner_id: int, project: str) -> list[dict]:
    """Editable requirements (chunks) for one of the owner's BRDs, in document order."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """select c.id, c.req_id, c.section, c.ordinal, c.text
               from brd_chunk c join brd_document d on d.id = c.document_id
               where d.owner_id = %s and d.project = %s
               order by c.ordinal""",
            (owner_id, project),
        )
        return [{"chunk_id": r[0], "req_id": r[1], "section": r[2], "ordinal": r[3], "text": r[4]}
                for r in cur.fetchall()]


def requirement_history(owner_id: int, chunk_id: int) -> list[dict]:
    """Audit trail for one requirement (owner-scoped), newest first."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """select rc.id, rc.kind, rc.old_text, rc.new_text, rc.changed_at, u.email
               from requirement_change rc
               join brd_document d on d.id = rc.document_id
               left join app_user u on u.id = rc.changed_by
               where rc.chunk_id = %s and d.owner_id = %s
               order by rc.changed_at desc""",
            (chunk_id, owner_id),
        )
        return [{"id": r[0], "kind": r[1], "old_text": r[2], "new_text": r[3],
                 "changed_at": r[4].isoformat(), "changed_by": r[5]} for r in cur.fetchall()]


# --- mutations --------------------------------------------------------------

def update_requirement(owner_id: int, chunk_id: int, new_text: str,
                       changed_by: int | None = None, kind: str = "edit",
                       base_hash: str | None = None) -> dict:
    """Replace a requirement's text. If the new text exceeds the chunk ceiling it is
    SPLIT: the first piece stays in this chunk (keeps its id/history), the rest are
    inserted right after. Runs as ONE locked transaction (baseline + write + audit),
    so a concurrent editor of the same BRD queues rather than colliding on ordinals.
    `base_hash` (optional) enables optimistic concurrency — a mismatch raises
    StaleRequirement. Returns {chunk_id, req_id, project, changed, chunks}."""
    new_text = (new_text or "").strip()
    if not new_text:
        raise ValueError("new text is empty")

    with pool().connection() as conn, conn.cursor() as cur:      # resolve doc + early no-op (no embed)
        cur.execute(
            """select c.text, c.document_id, d.project
               from brd_chunk c join brd_document d on d.id = c.document_id
               where c.id = %s and d.owner_id = %s""",
            (chunk_id, owner_id),
        )
        row = cur.fetchone()
    if not row:
        raise ChunkNotFound(chunk_id)
    old_text, document_id, project = row
    if base_hash is None and _hash(old_text or "") == _hash(new_text):
        return {"chunk_id": chunk_id, "req_id": None, "project": project, "changed": False, "chunks": 1}

    pieces = _split(new_text)                       # 1, or several if over the ceiling
    vectors, _tokens = embed_documents(pieces)      # OUTSIDE the write tx

    with pool().connection() as conn, conn.cursor() as cur:
        _lock_doc(cur, document_id)
        _defer_ordinals(cur)
        ensure_baseline_tx(cur, document_id, changed_by)         # baseline in the SAME tx as the edit
        req_id, changed = _op_edit(cur, document_id, owner_id, chunk_id, new_text,
                                   pieces, vectors, base_hash, changed_by, kind)
        conn.commit()

    if changed:
        cache.bust_project(project)
    return {"chunk_id": chunk_id, "req_id": req_id, "project": project, "changed": changed,
            "chunks": len(pieces) if changed else 1}


def split_requirement(owner_id: int, chunk_id: int, rows: list[str],
                      changed_by: int | None = None) -> dict:
    """Split one flattened chunk into several row-chunks (QA "make each row editable").

    Used to repair a table that was ingested as one un-delimited blob: `rows` are
    the reconstructed rows (literal substrings — see ingest.structure). The first
    row stays in this chunk (keeps its id + audit history); the rest are inserted
    right after, shifting later ordinals. Each row is embedded OUTSIDE the tx, so
    every row becomes its own retrievable, individually-editable requirement.
    Returns {chunk_id, project, rows}."""
    rows = [r.strip() for r in (rows or []) if r and r.strip()]
    if len(rows) < 2:
        raise ValueError("need at least two rows to split")

    with pool().connection() as conn, conn.cursor() as cur:      # resolve doc (no embed if not owned)
        cur.execute(
            """select c.document_id, d.project from brd_chunk c
               join brd_document d on d.id = c.document_id
               where c.id = %s and d.owner_id = %s""",
            (chunk_id, owner_id),
        )
        row = cur.fetchone()
    if not row:
        raise ChunkNotFound(chunk_id)
    document_id, project = row

    vectors, _tokens = embed_documents(rows)         # OUTSIDE the write tx

    with pool().connection() as conn, conn.cursor() as cur:
        _lock_doc(cur, document_id)
        _defer_ordinals(cur)
        ensure_baseline_tx(cur, document_id, changed_by)
        cur.execute(                                 # re-read under the lock (ordinal is authoritative here)
            "select text, req_id, section, ordinal from brd_chunk where id = %s and document_id = %s",
            (chunk_id, document_id),
        )
        r = cur.fetchone()
        if not r:
            raise ChunkNotFound(chunk_id)
        old_text, req_id, section, ordinal = r
        cur.execute(
            "update brd_chunk set ordinal = ordinal + %s where document_id = %s and ordinal > %s",
            (len(rows) - 1, document_id, ordinal),
        )
        cur.execute(
            "update brd_chunk set text = %s, token_count = %s, content_hash = %s where id = %s",
            (rows[0], _est_tokens(rows[0]), _hash(rows[0]), chunk_id),
        )
        cur.execute(
            """insert into brd_embedding (chunk_id, model, embedding) values (%s, %s, %s::vector)
               on conflict (chunk_id) do update set model = excluded.model, embedding = excluded.embedding""",
            (chunk_id, settings.embed_model_docs, _vec_literal(vectors[0])),
        )
        for i, piece in enumerate(rows[1:], start=1):
            _insert_chunk(cur, document_id, req_id, section, ordinal + i, piece, _vec_literal(vectors[i]))
        _audit(cur, document_id, chunk_id, req_id, old_text, rows[0], "split", changed_by)
        conn.commit()

    cache.bust_project(project)
    return {"chunk_id": chunk_id, "project": project, "rows": len(rows)}


def add_requirement(owner_id: int, project: str, text: str, req_id: str | None = None,
                    section: str | None = None, after_chunk_id: int | None = None,
                    changed_by: int | None = None) -> dict:
    """Add a new requirement — after a given chunk, or appended to the BRD. Splits if
    over the chunk ceiling. Returns {chunk_id (first piece), req_id, project, added}."""
    text = (text or "").strip()
    if not text:
        raise ValueError("text is empty")

    with pool().connection() as conn, conn.cursor() as cur:      # resolve + own the target doc (no embed if absent)
        if after_chunk_id is not None:
            cur.execute(
                """select c.document_id from brd_chunk c
                   join brd_document d on d.id = c.document_id
                   where c.id = %s and d.owner_id = %s and d.project = %s""",
                (after_chunk_id, owner_id, project),
            )
            r = cur.fetchone()
            if not r:
                raise ChunkNotFound(after_chunk_id)
            document_id = r[0]
        else:
            cur.execute(
                "select id from brd_document where owner_id = %s and project = %s order by id desc limit 1",
                (owner_id, project),
            )
            r = cur.fetchone()
            if not r:
                raise ChunkNotFound(project)
            document_id = r[0]

    pieces = _split(text)
    vectors, _tokens = embed_documents(pieces)      # OUTSIDE the write tx

    with pool().connection() as conn, conn.cursor() as cur:
        _lock_doc(cur, document_id)
        _defer_ordinals(cur)
        ensure_baseline_tx(cur, document_id, changed_by)
        first_id = _op_add(cur, document_id, text, pieces, vectors, req_id, section, after_chunk_id, changed_by)
        conn.commit()

    cache.bust_project(project)
    return {"chunk_id": first_id, "req_id": req_id, "project": project, "added": len(pieces)}


def revert_requirement(owner_id: int, chunk_id: int, changed_by: int | None = None) -> dict:
    """Safety net: restore a requirement to its PREVIOUS text (undo the last edit),
    from the audit trail. Re-embeds/re-indexes/busts like any edit and records a
    'revert' audit row. Returns {reverted, ...}. No-op if there's no prior version."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """select rc.old_text from requirement_change rc
               join brd_document d on d.id = rc.document_id
               where rc.chunk_id = %s and d.owner_id = %s
                 and rc.old_text is not null and rc.old_text <> ''
               order by rc.changed_at desc limit 1""",
            (chunk_id, owner_id),
        )
        row = cur.fetchone()
    if not row:
        return {"reverted": False, "chunk_id": chunk_id}
    res = update_requirement(owner_id, chunk_id, row[0], changed_by=changed_by, kind="revert")
    return {"reverted": res.get("changed", False), **res}


def remove_requirement(owner_id: int, chunk_id: int, changed_by: int | None = None,
                       base_hash: str | None = None) -> dict:
    """Delete a requirement (its embedding cascades). Ordinals keep their gaps — order
    is preserved and no shift is needed. Runs as one locked transaction (baseline +
    audit + delete). `base_hash` (optional) enables optimistic concurrency — a
    mismatch raises StaleRequirement. Returns {removed, req_id, project}."""
    with pool().connection() as conn, conn.cursor() as cur:      # resolve doc (no lock yet)
        cur.execute(
            """select c.document_id, d.project
               from brd_chunk c join brd_document d on d.id = c.document_id
               where c.id = %s and d.owner_id = %s""",
            (chunk_id, owner_id),
        )
        row = cur.fetchone()
    if not row:
        raise ChunkNotFound(chunk_id)
    document_id, project = row

    with pool().connection() as conn, conn.cursor() as cur:
        _lock_doc(cur, document_id)
        ensure_baseline_tx(cur, document_id, changed_by)
        req_id = _op_delete(cur, document_id, owner_id, chunk_id, base_hash, changed_by)
        conn.commit()

    cache.bust_project(project)
    return {"removed": True, "req_id": req_id, "project": project}


def _derive_key(project: str, operations: list[dict]) -> str:
    """Stable idempotency key for an apply, so a retried or double-submitted IDENTICAL
    plan dedupes: byte-identical operations ⇒ same key ⇒ the stored result is replayed
    instead of re-applied (which would duplicate 'add's). A genuinely different change
    (even by one requirement) yields a different key and applies normally."""
    canon = json.dumps({"project": project, "ops": operations}, sort_keys=True,
                        ensure_ascii=False, default=str)
    return "auto:" + _hash(canon)


def _read_change_result(owner_id: int, key: str) -> dict | None:
    """The stored apply summary for a committed change (or None if absent), tagged
    idempotent_replay so a caller can tell a replay from a fresh apply."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select result from change_request where owner_id = %s and idempotency_key = %s",
                    (owner_id, key))
        row = cur.fetchone()
    if not row:
        return None
    return {**(row[0] or {}), "idempotent_replay": True}


def apply_change_set(owner_id: int, project: str, operations: list[dict],
                     changed_by: int | None = None, idempotency_key: str | None = None,
                     story: str | None = None, refined: str | None = None,
                     model: str | None = None) -> dict:
    """Apply a reviewed set of operations to a BRD as ONE atomic transaction.

    This is the durable core of the story-driven "change requirements" feature. A
    plan of edits/adds/deletes is a single unit of intent, so it commits or rolls
    back as a whole — a mid-batch failure (missing chunk, optimistic-concurrency
    conflict, DB error, crash) leaves the BRD byte-for-byte as it was, never
    half-changed. Guarantees:

      * embed-outside-the-lock: every piece of every edit/add is embedded up front
        in ONE batch call, before the write tx opens (no slow network under a lock);
      * atomic + serialized: a single tx takes a per-document advisory lock, captures
        the review baseline, and applies all ops in order — concurrent edits to the
        same BRD queue instead of corrupting ordinals;
      * optimistic concurrency: each edit/delete carries the hash of the text it was
        planned against; if the live text moved, StaleRequirement aborts the whole set;
      * one cache bust, on success only;
      * idempotent + provenance: the change is recorded as a change_request row
        (story/refined/model/author) INSIDE the same tx, keyed on (owner_id,
        idempotency_key). A retry with the same key replays the stored result instead
        of re-applying — so a duplicated submit can't add a requirement twice — and
        the row exists only if the tx committed, so a rolled-back attempt retries fresh.

    Raises ChunkNotFound / StaleRequirement / VoyageUnavailable (never a partial
    apply). Returns {applied, edited, added, deleted, results, change_id} — or the
    stored result + idempotent_replay=True on a duplicate."""
    ops = list(operations or [])
    if not ops:
        return {"applied": 0, "edited": 0, "added": 0, "deleted": 0, "results": []}
    if len(ops) > MAX_BATCH_OPS:
        raise ValueError(f"too many operations in one change (max {MAX_BATCH_OPS})")

    # Prepare + validate every op first (DB-free: splitting and hashing are pure), so
    # a malformed plan fails fast before we touch the pool or spend an embed call. The
    # optimistic-concurrency base is the hash of the text the plan was built on
    # (op["old_text"]); absent ⇒ no check (e.g. a blind add). Pieces are collected for
    # a single embedding round-trip.
    plan: list[dict] = []
    all_pieces: list[str] = []
    for op in ops:
        if not isinstance(op, dict):
            raise ValueError("operation must be an object")
        kind = op.get("op")
        if kind == "edit":
            if not isinstance(op.get("chunk_id"), int):
                raise ValueError("edit op missing chunk_id")
            new_text = (op.get("new_text") or "").strip()
            if not new_text:
                raise ValueError("edit op has empty new_text")
            pieces = _split(new_text)
            plan.append({"op": "edit", "chunk_id": op["chunk_id"], "new_text": new_text, "pieces": pieces,
                         "at": len(all_pieces), "base_hash": _hash(op["old_text"]) if op.get("old_text") else None})
            all_pieces.extend(pieces)
        elif kind == "add":
            new_text = (op.get("new_text") or "").strip()
            if not new_text:
                raise ValueError("add op has empty new_text")
            pieces = _split(new_text)
            after = op.get("after_chunk_id")
            plan.append({"op": "add", "new_text": new_text, "pieces": pieces, "at": len(all_pieces),
                         "req_id": (op.get("req_id") or None), "section": (op.get("section") or None),
                         "after_chunk_id": after if isinstance(after, int) else None})
            all_pieces.extend(pieces)
        elif kind == "delete":
            if not isinstance(op.get("chunk_id"), int):
                raise ValueError("delete op missing chunk_id")
            plan.append({"op": "delete", "chunk_id": op["chunk_id"],
                         "base_hash": _hash(op["old_text"]) if op.get("old_text") else None})
        else:
            raise ValueError(f"unknown op: {kind!r}")

    key = (idempotency_key or "").strip() or _derive_key(project, operations)

    # Fast-path replay: if this exact change already committed, return its stored
    # result without re-embedding or re-writing. (A true concurrent race is still
    # caught by the unique constraint inside the tx below.)
    existing = _read_change_result(owner_id, key)
    if existing is not None:
        return existing

    # The BRD is the latest document for this owner+project — the same identity the
    # versioning/baseline layer uses. Resolve it once; all ops target it.
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select id from brd_document where owner_id = %s and project = %s order by id desc limit 1",
                    (owner_id, project))
        r = cur.fetchone()
    if not r:
        raise ChunkNotFound(project)
    document_id = r[0]

    vectors, _tokens = embed_documents(all_pieces) if all_pieces else ([], 0)   # OUTSIDE the write tx

    edited = added = deleted = 0
    results: list[dict] = []
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            _lock_doc(cur, document_id)
            _defer_ordinals(cur)
            # Anchor the change set FIRST. The unique (owner_id, idempotency_key) index
            # makes a racing duplicate fail here → the whole tx rolls back (nothing
            # applied), and we replay the winner's result below.
            cur.execute(
                """insert into change_request
                     (owner_id, document_id, project, story, refined, model, idempotency_key)
                   values (%s, %s, %s, %s, %s, %s, %s) returning id""",
                (owner_id, document_id, project, story, refined, model, key),
            )
            change_id = cur.fetchone()[0]
            ensure_baseline_tx(cur, document_id, changed_by)
            for p in plan:
                if p["op"] == "edit":
                    vs = vectors[p["at"]: p["at"] + len(p["pieces"])]
                    _rid, changed = _op_edit(cur, document_id, owner_id, p["chunk_id"], p["new_text"],
                                             p["pieces"], vs, p["base_hash"], changed_by, change_id=change_id)
                    edited += 1 if changed else 0
                    results.append({"op": "edit", "chunk_id": p["chunk_id"], "ok": True, "changed": changed})
                elif p["op"] == "add":
                    vs = vectors[p["at"]: p["at"] + len(p["pieces"])]
                    cid = _op_add(cur, document_id, p["new_text"], p["pieces"], vs, p["req_id"],
                                  p["section"], p["after_chunk_id"], changed_by, change_id=change_id)
                    added += 1
                    results.append({"op": "add", "chunk_id": cid, "ok": True})
                elif p["op"] == "delete":
                    _op_delete(cur, document_id, owner_id, p["chunk_id"], p["base_hash"], changed_by,
                               change_id=change_id)
                    deleted += 1
                    results.append({"op": "delete", "chunk_id": p["chunk_id"], "ok": True})
            summary = {"applied": edited + added + deleted, "edited": edited, "added": added,
                       "deleted": deleted, "results": results}
            cur.execute("update change_request set result = %s, applied = %s where id = %s",
                        (Jsonb(summary), summary["applied"], change_id))
            conn.commit()
    except UniqueViolation:
        replay = _read_change_result(owner_id, key)
        return replay if replay is not None else {"applied": 0, "edited": 0, "added": 0,
                                                  "deleted": 0, "results": [], "idempotent_replay": True}

    cache.bust_project(project)
    return {**summary, "change_id": change_id}


def list_change_requests(owner_id: int, project: str, limit: int = 50) -> list[dict]:
    """Provenance feed for a BRD: recent change requests, newest first, each with the
    story that drove it, who applied it, and how many requirements it touched."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """select cr.id, cr.story, cr.refined, cr.model, cr.applied, cr.created_at, u.email,
                      (select count(*) from requirement_change rc where rc.change_id = cr.id)
               from change_request cr
               join brd_document d on d.id = cr.document_id
               left join app_user u on u.id = cr.owner_id
               where d.owner_id = %s and d.project = %s
               order by cr.created_at desc limit %s""",
            (owner_id, project, limit),
        )
        return [{"id": r[0], "story": r[1], "refined": r[2], "model": r[3], "applied": r[4],
                 "created_at": r[5].isoformat(), "author": r[6], "changes": r[7]}
                for r in cur.fetchall()]

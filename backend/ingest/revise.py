"""BRD revisions: a re-upload is COMPARED with the live BRD, then applied or discarded by
the user — and whatever changes, requirement ids survive and nothing is hard-deleted.

  align(live, new)      pairs new requirements with live ones: exact content -> same
                        requirement id -> text similarity (Arabic-normalized word Jaccard)
  compare(...)          stages a brd_revision: every new requirement is same / modified /
                        added, every unmatched live one is removed; each item carries its
                        impact (use cases / tested use cases citing it)
  apply_revision(...)   makes the live BRD equal to the new version, honouring the user's
                        intent: keep a requirement the file dropped, skip a change, skip an
                        addition. Refuses (Stale) if the live BRD changed since the compare.
  reconcile(...)        the shared id-preserving writer (also used by snapshot restore):
                        same -> reordered in place; modified -> updated in place (same id);
                        added -> inserted; removed -> ARCHIVED via archive.retire (same id,
                        resolvable, restorable). Every change writes requirement_change.

Everything runs in one transaction with the document row locked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from psycopg.types.json import Jsonb

from backend.core import cache
from backend.core.config import settings
from backend.core.db import pool
from . import archive
from .chunker import CHUNKER_VERSION

_SIM_MIN = 0.45                 # below this a pairing is a guess, not the same requirement
_MAX_BUCKET_FACTOR = 8          # prefilter guard: a word shared by more than len(live)//this is too common to anchor on
_SEED_RAREST_K = 5              # always seed candidates from a row's k rarest words, even past the bucket cap
_REPLACE_MIN_LIVE = 5           # the replace-guard only applies once a BRD has a meaningful size
_REPLACE_MIN_RATIO = 0.20       # below this fraction of live requirements matched, apply demands confirmation


class RevisionError(ValueError):
    """A caller error (not found, wrong state)."""


class Stale(RevisionError):
    """The live BRD changed after the comparison — re-compare before applying."""


class NeedsConfirmation(RevisionError):
    """Applying this revision would archive almost the whole live BRD — the usual sign of
    the wrong file or unreadable text, not an intentional near-total rewrite. The caller
    must re-call with confirm_replace=True (an explicit "Replace everything" choice) to
    proceed. `.summary` carries the counts the UI needs to show that choice."""
    def __init__(self, summary: dict):
        self.summary = summary
        super().__init__(
            f"Only {summary['matched']} of {summary['live']} current requirements match this file — "
            f"applying it replaces almost the whole BRD. Confirm to continue.")


# --- pure ----------------------------------------------------------------------------

_AR_DIACRITICS = re.compile(r"[ً-ٰٟـ]")


def _words(text: str) -> set[str]:
    t = _AR_DIACRITICS.sub("", (text or "").lower())
    t = t.translate(str.maketrans("أإآٱىة", "اااايه"))
    return {w for w in re.findall(r"\w+", t) if len(w) > 1}


def similarity(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if (wa or wb) else 1.0


def _row_text(text: str) -> str:
    """For a table-row chunk (chunker._split_table prefixes every row with the shared
    table header/preamble so each row carries column context — see chunker.py), alignment
    must judge similarity on the DATA ROW alone: two unrelated rows of the same table share
    that header/preamble vocabulary and otherwise look confusingly similar.

    Detected STRUCTURALLY, not via chunker._looks_table's "60% of lines are piped" heuristic:
    that heuristic misses a row chunk whose preamble has 2+ intro paragraphs (lines), where
    only 2 of 4+ lines are piped and the shared preamble still makes unrelated rows look
    similar. chunker._split_table always emits `[preamble...], header, data-row` (the `---`
    separator is dropped before the row is built) — so a table-row chunk's last two non-empty
    lines are always `header` then the data row itself; whichever text has that shape is
    judged on its last line alone. Non-table chunks (most requirements) are returned
    unchanged — including a prose requirement that merely ENDS in a small table: it keeps its
    `|---|` separator and/or several rows, so it has more than two piped lines and is compared
    on its full text (cutting it to its last row would turn "a row was added to its table"
    into removed + added and lose the requirement id)."""
    lines = [l for l in (text or "").splitlines() if l.strip()]
    piped = [i for i, l in enumerate(lines) if "|" in l]
    if len(piped) == 2 and piped == [len(lines) - 2, len(lines) - 1]:
        return lines[-1]
    return text or ""


def _align_words(text: str) -> set[str]:
    return _words(_row_text(text))


def align(live: list[dict], new: list[dict]) -> dict[int, tuple[int, float]]:
    """new index -> (live index, similarity). Each live row is used at most once.
    Rows need: content_hash, text, req_id, section."""
    pairs: dict[int, tuple[int, float]] = {}
    used: set[int] = set()
    by_hash: dict[str, list[int]] = {}
    for i, r in enumerate(live):
        by_hash.setdefault(r["content_hash"], []).append(i)
    for j, r in enumerate(new):                                    # 1. identical content
        for i in by_hash.get(r["content_hash"], []):
            if i not in used:
                pairs[j] = (i, 1.0)
                used.add(i)
                break

    def unique_ids(rows, idx):
        seen: dict[str, list[int]] = {}
        for k in idx:
            if rows[k].get("req_id"):
                seen.setdefault(rows[k]["req_id"], []).append(k)
        return {rid: ks[0] for rid, ks in seen.items() if len(ks) == 1}

    live_ids = unique_ids(live, [i for i in range(len(live)) if i not in used])
    new_ids = unique_ids(new, [j for j in range(len(new)) if j not in pairs])
    for rid, j in new_ids.items():                                 # 2. same requirement id
        i = live_ids.get(rid)
        if i is not None and i not in used:
            pairs[j] = (i, similarity(live[i]["text"], new[j]["text"]))
            used.add(i)

    # 3. similar text. Each text is tokenized ONCE (not per-pair) and candidates are
    # prefiltered through an inverted index on shared words, so typical cost stays near
    # the size of the two lists instead of O(live × new) (a full-matrix Jaccard measured
    # ~35 s at 600×600, ~10 min extrapolated at the ingest chunk ceiling). A word shared by
    # too many live rows (near-stopword for this document) is too common to anchor a
    # candidate on and is skipped, same as a real inverted index would.
    rem_new = [j for j in range(len(new)) if j not in pairs]
    rem_live = [i for i in range(len(live)) if i not in used]
    if rem_new and rem_live:
        live_words = {i: _align_words(live[i]["text"]) for i in rem_live}
        new_words = {j: _align_words(new[j]["text"]) for j in rem_new}
        index: dict[str, list[int]] = {}
        for i in rem_live:
            for w in live_words[i]:
                index.setdefault(w, []).append(i)
        max_bucket = max(20, len(rem_live) // _MAX_BUCKET_FACTOR)
        cand = []
        for j in rem_new:
            wj = new_words[j]
            if not wj:
                continue
            candidates_i: set[int] = set()
            # Always seed from the k RAREST words of this row, regardless of the bucket cap.
            # A row whose every shared word happens to be common in THIS document (e.g. every
            # cell value of a big field-permutation table is reused across dozens of rows) would
            # otherwise get zero candidates from the capped loop below and silently fall through
            # to added/removed instead of being matched — reproduced with a 120-row field table
            # (4 names x 3 types x yes/no x 5 lengths): every word's bucket exceeded the cap, so
            # the capped-only prefilter matched 0/120 where exhaustive matched 120/120.
            rarest = sorted((w for w in wj if w in index), key=lambda w: len(index[w]))[:_SEED_RAREST_K]
            for w in rarest:
                candidates_i.update(index[w])
            for w in wj:
                bucket = index.get(w)
                if bucket and len(bucket) <= max_bucket:
                    candidates_i.update(bucket)
            if not any(w in index for w in wj):
                continue                                 # shares no word with any live row: can't reach _SIM_MIN

            def score(ids) -> bool:
                hit = False
                for i in ids:
                    if i in used:
                        continue
                    wi = live_words[i]
                    union = len(wi | wj)
                    if not union:
                        continue
                    base = len(wi & wj) / union         # the threshold gate — base similarity ONLY
                    if base < _SIM_MIN:
                        continue
                    bonus = 0.05 if live[i].get("section") == new[j].get("section") else 0.0
                    cand.append((base + bonus, j, i))    # bonus only breaks ties; it never crosses _SIM_MIN
                    hit = True
                return hit

            if not score(candidates_i):
                # the seeded + capped candidates hold no pair above the threshold: the true match
                # may share only over-cap words — score the rest exactly rather than lose its id.
                score(i for i in rem_live if i not in candidates_i)
        for s, j, i in sorted(cand, reverse=True):
            if j not in pairs and i not in used:
                pairs[j] = (i, min(1.0, s))
                used.add(i)
    return pairs


def classify(live: list[dict], new: list[dict]) -> list[dict]:
    """Revision items: one per new row (same / modified / added) + one per unmatched live row."""
    pairs = align(live, new)
    items: list[dict] = []
    for j, r in enumerate(new):
        if j in pairs:
            i, sim = pairs[j]
            l = live[i]
            same = l["content_hash"] == r["content_hash"]
            items.append({"change": "same" if same else "modified", "ordinal": j, "live": l, "new": r,
                          "similarity": 1.0 if same else round(sim, 3)})
        else:
            items.append({"change": "added", "ordinal": j, "live": None, "new": r, "similarity": None})
    matched = {pairs[j][0] for j in pairs}
    for i, l in enumerate(live):
        if i not in matched:
            items.append({"change": "removed", "ordinal": None, "live": l, "new": None, "similarity": None})
    return items


# --- db helpers ------------------------------------------------------------------------

def _live(cur, document_id: int) -> list[dict]:
    cur.execute("select id, req_id, section, section_path, page, ordinal, text, content_hash, embed_hash "
                "from brd_chunk where document_id = %s order by ordinal", (document_id,))
    return [{"id": r[0], "req_id": r[1], "section": r[2], "section_path": r[3], "page": r[4], "ordinal": r[5],
             "text": r[6], "content_hash": r[7], "embed_hash": r[8]} for r in cur.fetchall()]


def _lock_doc(cur, owner_id: int, document_id: int) -> dict:
    """Lock a document for writing. Takes the SAME xact-scoped advisory lock requirement
    edits take first (backend/requirements/edit.py `_lock_doc`), keyed on document_id,
    BEFORE the row lock below — otherwise the two paths can take a row lock and an
    advisory lock in opposite orders and deadlock under concurrent writers."""
    cur.execute("select pg_advisory_xact_lock(%s)", (document_id,))
    cur.execute("select id, project, revision_no, source_id from brd_document "
                "where id = %s and owner_id = %s for update", (document_id, owner_id))
    r = cur.fetchone()
    if not r:
        raise RevisionError("BRD not found")
    return {"id": r[0], "project": r[1], "revision_no": r[2], "source_id": r[3]}


def _fingerprint(rows: list[dict]) -> frozenset:
    """Cheap equality check for 'did the live BRD change under us' between an unlocked
    read (done before the expensive alignment) and the locked re-read. Includes req_id and
    section (not just content_hash) so a metadata-only change — the text is untouched but its
    requirement id or section moved — still counts as a change and triggers a re-classify."""
    return frozenset((r["id"], r["content_hash"], r["req_id"], r["section"]) for r in rows)


def _is_stale(items: list[dict], live: dict[int, dict], hash_key: str = "live_hash") -> bool:
    """A staged revision is stale if the CURRENT live id set no longer matches exactly what
    its items reference at compare time. `classify()` gives every live row at compare time
    exactly one item (same/modified/removed carry live_chunk_id; added does not), so the set
    of non-null live_chunk_ids among the items IS the compare-time live id set. Checking only
    "does a referenced id still exist with the same hash" (the old check) misses a live id
    that is NOT referenced at all — one added or revived after the compare — and reconcile()
    would then silently archive it as "not kept". Comparing the full sets catches that too."""
    referenced = {it["live_chunk_id"] for it in items if it.get("live_chunk_id") is not None}
    if referenced != set(live.keys()):
        return True
    return any(it.get("live_chunk_id") is not None and live[it["live_chunk_id"]]["content_hash"] != it.get(hash_key)
               for it in items)


def _replace_gate(same: int, modified_total: int, kept_removed: int, removed_total: int, live: int) -> dict:
    """Shared replace-ratio math for apply_revision (actual skip/keep choices known) and
    get_open (no choices yet — an up-front warning with none skipped/kept). `matched` is
    what still matches the current live requirement set: unchanged + EVERY modified new row,
    applied or skipped (skipping a modification keeps the live row exactly as it is — it IS
    still matched, just under its old text, not archived) + removed ones the user kept."""
    matched = same + modified_total + kept_removed
    archived = removed_total - kept_removed
    ratio = (matched / live) if live else 1.0
    needs = live >= _REPLACE_MIN_LIVE and archived > 0 and ratio < _REPLACE_MIN_RATIO
    return {"matched": matched, "live": live, "ratio": round(ratio, 4), "needs_confirmation": needs}


def _vec_from_cache(cur, owner_id: int, model: str, embed_hash: str | None) -> str | None:
    if not embed_hash:
        return None
    cur.execute("select embedding::text from brd_embedding_cache where owner_id = %s and model = %s "
                "and input_hash = %s", (owner_id, model, embed_hash))
    r = cur.fetchone()
    return r[0] if r else None


def _audit(cur, document_id, chunk_id, req_id, old_text, new_text, kind, by):
    cur.execute(
        """insert into requirement_change (document_id, chunk_id, req_id, old_text, new_text, kind, changed_by)
           values (%s, %s, %s, %s, %s, %s, %s)""",
        (document_id, chunk_id, req_id, old_text, new_text or "", kind, by),
    )


def _defer(cur) -> None:
    cur.execute("set constraints brd_chunk_doc_ordinal_uk deferred")


@dataclass
class Target:
    """One requirement of the desired final state, in order."""
    text: str
    content_hash: str
    req_id: str | None = None
    section: str | None = None
    section_path: str | None = None
    page: int | None = None
    token_count: int | None = None
    live_id: int | None = None          # reuse this live row (same / modified)
    archived_id: int | None = None      # revive this archived row (restore)
    embed_hash: str | None = None
    vec: str | None = None              # pgvector literal to store when the vector changes


def reconcile(cur, document_id: int, targets: list[Target], *, reason: str, by: int | None,
              revision_id: int | None = None, model: str | None = None) -> dict:
    """Make the document's live requirements equal `targets` (in order), preserving ids.
    Live rows no target keeps are ARCHIVED (never deleted)."""
    model = model or settings.embed_model_docs
    _defer(cur)
    live = {r["id"]: r for r in _live(cur, document_id)}
    kept: set[int] = set()
    stats = {"kept": 0, "updated": 0, "added": 0, "revived": 0, "archived": 0}
    for pos, t in enumerate(targets):
        if t.archived_id is not None and archive.revive(cur, t.archived_id, ordinal=pos):
            stats["revived"] += 1
            kept.add(t.archived_id)
            _audit(cur, document_id, t.archived_id, t.req_id, None, t.text, "add", by)
            continue
        if t.live_id is not None and t.live_id in live:
            cur_row = live[t.live_id]
            kept.add(t.live_id)
            changed = cur_row["content_hash"] != t.content_hash
            cur.execute(
                """update brd_chunk set ordinal = %s, text = %s, content_hash = %s, token_count = %s,
                          req_id = %s, section = %s, section_path = %s, page = %s,
                          embed_hash = coalesce(%s, embed_hash)
                   where id = %s""",
                (pos, t.text, t.content_hash, t.token_count, t.req_id, t.section, t.section_path, t.page,
                 t.embed_hash if t.vec else None, t.live_id),
            )
            if t.vec:
                cur.execute(
                    """insert into brd_embedding (chunk_id, model, embedding) values (%s, %s, %s::vector)
                       on conflict (chunk_id) do update set model = excluded.model, embedding = excluded.embedding""",
                    (t.live_id, model, t.vec))
            if changed:
                stats["updated"] += 1
                _audit(cur, document_id, t.live_id, t.req_id, cur_row["text"], t.text, "edit", by)
            else:
                stats["kept"] += 1
            continue
        if not t.vec:
            raise RevisionError("a new requirement has no embedding — re-run the upload")
        cur.execute(
            """insert into brd_chunk (document_id, req_id, section, section_path, page, ordinal, text,
                                      token_count, content_hash, embed_hash)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
            (document_id, t.req_id, t.section, t.section_path, t.page, pos, t.text, t.token_count,
             t.content_hash, t.embed_hash),
        )
        new_id = cur.fetchone()[0]
        cur.execute("insert into brd_embedding (chunk_id, model, embedding) values (%s, %s, %s::vector)",
                    (new_id, model, t.vec))
        stats["added"] += 1
        _audit(cur, document_id, new_id, t.req_id, None, t.text, "add", by)
    for cid, row in live.items():
        if cid not in kept:
            _audit(cur, document_id, cid, row["req_id"], row["text"], "", "delete", by)   # audit first
            archive.retire(cur, cid, reason=reason, by=by, revision_id=revision_id)
            stats["archived"] += 1
    return stats


# --- compare (stage a revision) ----------------------------------------------------------

def compare(cur, *, owner_id: int, document_id: int, project: str, new_rows: list[dict],
            job_id: int | None, source_id: int | None,
            fence: Callable[[object], None] | None = None) -> dict:
    """Stage a revision for review (caller's tx). `new_rows` are the new version's chunks
    (text, content_hash, req_id, section, section_path, page, token_count, embed_hash).
    Supersedes any older open revision of the document.

    The alignment (classify) is the expensive step, so it runs BEFORE the document is
    locked, against an unlocked read of the live BRD; once locked, the live set is
    re-read and compared by (id, content_hash) — if nothing changed the unlocked result
    is reused, otherwise it is redone (now locked, so it can't race again).

    `fence` (optional): called with the cursor right after the document lock is taken and
    BEFORE any write, so a caller (the jobs runner) can check-and-raise — e.g. "this job's
    lease was lost or it was canceled" — inside the SAME transaction; raising there rolls
    everything in this transaction back, so nothing is written for a job that's no longer
    valid. Default None = no check (current behavior)."""
    live_before = _live(cur, document_id)
    items = classify(live_before, new_rows)
    doc = _lock_doc(cur, owner_id, document_id)
    if fence is not None:
        fence(cur)
    live_now = _live(cur, document_id)
    if _fingerprint(live_now) != _fingerprint(live_before):
        items = classify(live_now, new_rows)
    cur.execute("update brd_revision set status = 'superseded', resolved_at = now() "
                "where document_id = %s and status = 'review' returning job_id", (document_id,))
    superseded_jobs = [r[0] for r in cur.fetchall() if r[0] is not None]
    if superseded_jobs:                                 # don't leave the old revision's job hanging in 'review'
        cur.execute("update ingest_job set status = 'canceled', error = %s, "
                    "finished_at = coalesce(finished_at, now()), updated_at = now() "
                    "where id = any(%s) and status = 'review'",
                    ("Superseded by a newer version", superseded_jobs))
    summary = {k: sum(1 for it in items if it["change"] == k) for k in ("same", "modified", "added", "removed")}
    cur.execute(
        """insert into brd_revision (document_id, owner_id, project, job_id, source_id, base_revision_no, summary)
           values (%s, %s, %s, %s, %s, %s, %s) returning id""",
        (document_id, owner_id, project, job_id, source_id, doc["revision_no"], Jsonb(summary)),
    )
    rid = cur.fetchone()[0]
    _write_items(cur, rid, items)
    return {"revision_id": rid, "summary": summary}


def _write_items(cur, revision_id: int, items: list[dict]) -> None:
    rows = []
    for it in items:
        l, n = it["live"], it["new"]
        src = n or l
        rows.append((revision_id, it["change"], it["ordinal"], l["id"] if l else None,
                     l["content_hash"] if l else None, src.get("req_id"), src.get("section"),
                     src.get("section_path"), src.get("page"), n["text"] if n else None,
                     l["text"] if l and it["change"] in ("modified", "removed") else None,
                     n.get("token_count") if n else None, n["content_hash"] if n else None,
                     n.get("embed_hash") if n else None, it["similarity"]))
    cur.executemany(
        """insert into brd_revision_item (revision_id, change, ordinal, live_chunk_id, live_hash, req_id, section,
               section_path, page, text, old_text, token_count, content_hash, embed_hash, similarity)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)


def _new_rows(cur, revision_id: int) -> list[dict]:
    cur.execute("select text, content_hash, req_id, section, section_path, page, token_count, embed_hash "
                "from brd_revision_item where revision_id = %s and ordinal is not null order by ordinal",
                (revision_id,))
    return [{"text": r[0], "content_hash": r[1], "req_id": r[2], "section": r[3], "section_path": r[4],
             "page": r[5], "token_count": r[6], "embed_hash": r[7]} for r in cur.fetchall()]


def _peek_revision_doc(cur, owner_id: int, revision_id: int) -> int:
    """Unlocked lookup of a staged revision's document_id, so the caller can lock the
    DOCUMENT first — see _open_revision below for why the order matters."""
    cur.execute("select document_id from brd_revision where id = %s and owner_id = %s",
                (revision_id, owner_id))
    r = cur.fetchone()
    if not r:
        raise RevisionError("revision not found")
    return r[0]


def _open_revision(cur, owner_id: int, revision_id: int) -> dict:
    """Lock + validate the staged revision row. compare()/recompare() take the document's
    advisory lock FIRST and only then touch brd_revision rows (superseding/writing items);
    every caller here that also needs the document locked MUST do the same — lock the
    document (_lock_doc, via _peek_revision_doc for the id) BEFORE calling this, never after
    — otherwise the two paths can take the same two locks in opposite orders and deadlock."""
    cur.execute("select id, document_id, project, status, source_id from brd_revision "
                "where id = %s and owner_id = %s for update", (revision_id, owner_id))
    r = cur.fetchone()
    if not r:
        raise RevisionError("revision not found")
    if r[3] != "review":
        raise RevisionError(f"this revision is already {r[3]}")
    return {"id": r[0], "document_id": r[1], "project": r[2], "source_id": r[4]}


def recompare(owner_id: int, revision_id: int) -> dict:
    """Re-run the comparison of a staged revision against the CURRENT live BRD (after the
    live BRD was edited). No re-extraction or re-embedding. The expensive alignment runs
    against an unlocked read taken before any lock; the document is then locked (same order
    compare() uses — see _open_revision), and the live set is re-read and compared — only
    redone (now locked) if it actually changed between the unlocked and locked reads."""
    with pool().connection() as conn, conn.cursor() as cur:
        document_id = _peek_revision_doc(cur, owner_id, revision_id)
        live_before = _live(cur, document_id)
        new_before = _new_rows(cur, revision_id)
        items = classify(live_before, new_before)        # the expensive part — no lock held
        _lock_doc(cur, owner_id, document_id)
        rev = _open_revision(cur, owner_id, revision_id)
        new_rows = _new_rows(cur, revision_id)
        live_now = _live(cur, document_id)
        if (_fingerprint(live_now) != _fingerprint(live_before)
                or [r["content_hash"] for r in new_rows] != [r["content_hash"] for r in new_before]):
            items = classify(live_now, new_rows)         # changed meanwhile: redo under the lock
        cur.execute("delete from brd_revision_item where revision_id = %s", (revision_id,))
        _write_items(cur, revision_id, items)
        summary = {k: sum(1 for it in items if it["change"] == k) for k in ("same", "modified", "added", "removed")}
        cur.execute("update brd_revision set summary = %s where id = %s", (Jsonb(summary), revision_id))
        conn.commit()
    return {"revision_id": revision_id, "summary": summary}


# --- read -------------------------------------------------------------------------------

def get_open(owner_id: int, project: str) -> dict | None:
    """The BRD's revision awaiting review, with items and each item's impact on use cases."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "select r.id, r.document_id, r.summary, r.created_at, r.base_revision_no, s.filename, s.id "
            "from brd_revision r left join brd_source s on s.id = r.source_id "
            "where r.owner_id = %s and r.project = %s and r.status = 'review' order by r.id desc limit 1",
            (owner_id, project))
        r = cur.fetchone()
        if not r:
            return None
        rev = {"id": r[0], "document_id": r[1], "summary": r[2], "created_at": r[3].isoformat(),
               "base_revision_no": r[4], "filename": r[5], "source_id": r[6]}
        cur.execute(
            "select id, change, ordinal, live_chunk_id, live_hash, req_id, section, section_path, page, text, "
            "old_text, similarity from brd_revision_item where revision_id = %s "
            "order by coalesce(ordinal, 1000000), id", (rev["id"],))
        items = [{"id": x[0], "change": x[1], "ordinal": x[2], "live_chunk_id": x[3], "req_id": x[5],
                  "section": x[6], "section_path": x[7], "page": x[8], "text": x[9], "old_text": x[10],
                  "similarity": x[11], "_live_hash": x[4]} for x in cur.fetchall()]
        # impact: use cases (and tested ones) citing each live requirement that would change
        touched = [it["live_chunk_id"] for it in items if it["live_chunk_id"] and it["change"] in ("modified", "removed")]
        impact: dict[int, tuple[int, int]] = {}
        if touched:
            cur.execute(
                "select c.cid, count(uc.id), count(uc.id) filter (where uc.test_status <> 'not_tested') "
                "from unnest(%s::bigint[]) c(cid) left join use_case uc on uc.owner_id = %s and uc.project = %s "
                "  and uc.source_chunk_ids @> to_jsonb(array[c.cid]) group by c.cid",
                (touched, owner_id, project))
            impact = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
        cur.execute("select id, content_hash from brd_chunk where document_id = %s", (rev["document_id"],))
        live_hash = dict(cur.fetchall())
    live_map = {i: {"content_hash": h} for i, h in live_hash.items()}
    stale = _is_stale(items, live_map, hash_key="_live_hash")
    same_n = sum(1 for it in items if it["change"] == "same")
    modified_n = sum(1 for it in items if it["change"] == "modified")
    removed_n = sum(1 for it in items if it["change"] == "removed")
    gate = _replace_gate(same_n, modified_n, 0, removed_n, len(live_hash))
    for it in items:
        uc, tested = impact.get(it["live_chunk_id"], (0, 0))
        it["use_cases"], it["tested_use_cases"] = uc, tested
        it.pop("_live_hash", None)
    rev["items"], rev["stale"] = items, stale
    rev["needs_confirmation"], rev["replace_ratio"] = gate["needs_confirmation"], gate["ratio"]
    return rev


# --- resolve ----------------------------------------------------------------------------

def apply_revision(owner_id: int, revision_id: int, *, keep_removed: list[int] | None = None,
                   skip: list[int] | None = None, by: int | None = None,
                   confirm_replace: bool = False) -> dict:
    """Apply a staged revision, honouring the user's intent:
       keep_removed — item ids of REMOVED requirements to keep (not archived)
       skip         — item ids of MODIFIED (keep the current text) or ADDED (don't add) changes
    A pre-apply snapshot is taken (one-click undo in Versions); an approved BRD is flipped to
    draft, same as a requirement edit. Raises Stale if the live BRD changed after the
    comparison, or NeedsConfirmation if this would archive almost the whole BRD and
    confirm_replace wasn't set."""
    from backend.requirements import versioning

    keep_removed, skip = set(keep_removed or []), set(skip or [])
    model = settings.embed_model_docs
    with pool().connection() as conn, conn.cursor() as cur:
        document_id = _peek_revision_doc(cur, owner_id, revision_id)
        doc = _lock_doc(cur, owner_id, document_id)        # document lock BEFORE the revision row — see _open_revision
        rev = _open_revision(cur, owner_id, revision_id)
        cur.execute("select id, change, ordinal, live_chunk_id, live_hash, text, content_hash, req_id, section, "
                    "section_path, page, token_count, embed_hash from brd_revision_item where revision_id = %s",
                    (revision_id,))
        items = [dict(zip(("id", "change", "ordinal", "live_chunk_id", "live_hash", "text", "content_hash",
                           "req_id", "section", "section_path", "page", "token_count", "embed_hash"), r))
                 for r in cur.fetchall()]
        live = {r["id"]: r for r in _live(cur, doc["id"])}
        if _is_stale(items, live):
            raise Stale("The BRD was edited after this comparison — re-compare before applying.")

        removed_items = [it for it in items if it["change"] == "removed"]
        modified_items = [it for it in items if it["change"] == "modified"]
        same_n = sum(1 for it in items if it["change"] == "same")
        kept_removed_n = sum(1 for it in removed_items if it["id"] in keep_removed)
        gate = _replace_gate(same_n, len(modified_items), kept_removed_n, len(removed_items), len(live))
        if not confirm_replace and gate["needs_confirmation"]:
            raise NeedsConfirmation({"same": same_n, "modified": len(modified_items),
                                     "added": sum(1 for it in items if it["change"] == "added"),
                                     "removed": len(removed_items), "live": gate["live"],
                                     "matched": gate["matched"], "matched_ratio": gate["ratio"]})

        versioning.ensure_baseline_tx(cur, doc["id"], by)       # approved -> draft, same as an edit
        versioning._snapshot(cur, doc["id"], "auto", f"Before applying revision #{revision_id}", by)

        new_items = sorted((it for it in items if it["ordinal"] is not None), key=lambda it: it["ordinal"])
        targets: list[Target] = []
        for it in new_items:
            if it["change"] == "added" and it["id"] in skip:
                continue
            if it["change"] == "modified" and it["id"] in skip:
                l = live[it["live_chunk_id"]]                  # keep the current requirement as is
                targets.append(Target(text=l["text"], content_hash=l["content_hash"], req_id=l["req_id"],
                                      section=l["section"], section_path=l["section_path"], page=l["page"],
                                      live_id=l["id"]))
                continue
            vec = None
            live_row = live.get(it["live_chunk_id"]) if it["live_chunk_id"] else None
            if it["change"] != "same" or (live_row and live_row["embed_hash"] != it["embed_hash"]):
                vec = _vec_from_cache(cur, owner_id, model, it["embed_hash"])
                if vec is None and it["change"] != "same":
                    raise RevisionError("embeddings for this revision are missing — upload the file again")
            targets.append(Target(text=it["text"], content_hash=it["content_hash"], req_id=it["req_id"],
                                  section=it["section"], section_path=it["section_path"], page=it["page"],
                                  token_count=it["token_count"], live_id=it["live_chunk_id"],
                                  embed_hash=it["embed_hash"], vec=vec))
        # removed requirements the user keeps: re-inserted after their old predecessor
        old_order = [r["id"] for r in sorted(live.values(), key=lambda r: r["ordinal"])]
        for it in items:
            if it["change"] == "removed" and it["id"] in keep_removed:
                l = live[it["live_chunk_id"]]
                keep = Target(text=l["text"], content_hash=l["content_hash"], req_id=l["req_id"],
                              section=l["section"], section_path=l["section_path"], page=l["page"], live_id=l["id"])
                prev = old_order[:old_order.index(l["id"])]
                pos = 0
                for k, t in enumerate(targets):
                    if t.live_id in prev:
                        pos = k + 1
                targets.insert(pos, keep)

        stats = reconcile(cur, doc["id"], targets, reason="revision", by=by, revision_id=revision_id, model=model)
        cur.execute("select sha256 from brd_source where id = %s", (rev["source_id"],))
        sha = (cur.fetchone() or [None])[0]
        cur.execute("update brd_document set revision_no = revision_no + 1, source_id = coalesce(%s, source_id), "
                    "sha256 = coalesce(%s, sha256), updated_at = now(), status = 'ready', last_error = null, "
                    "chunker_version = %s where id = %s", (rev["source_id"], sha, CHUNKER_VERSION, doc["id"]))
        resolution = {**stats, "kept_removed": sorted(keep_removed), "skipped": sorted(skip)}
        cur.execute("update brd_revision set status = 'applied', resolved_at = now(), resolved_by = %s, "
                    "resolution = %s where id = %s", (by, Jsonb(resolution), revision_id))
        cur.execute("update ingest_job set status = 'done', finished_at = coalesce(finished_at, now()), "
                    "updated_at = now() where id = (select job_id from brd_revision where id = %s) "
                    "and status = 'review'", (revision_id,))
        conn.commit()
    cache.bust_project(rev["project"], owner_id=owner_id)
    return {"revision_id": revision_id, "project": rev["project"], **resolution}


def discard_revision(owner_id: int, revision_id: int, *, by: int | None = None) -> dict:
    """Throw the staged revision away; the live BRD is untouched (its source file stays in
    storage with the BRD, as the record of what was uploaded)."""
    with pool().connection() as conn, conn.cursor() as cur:
        document_id = _peek_revision_doc(cur, owner_id, revision_id)
        _lock_doc(cur, owner_id, document_id)              # document lock BEFORE the revision row — see _open_revision
        rev = _open_revision(cur, owner_id, revision_id)
        cur.execute("update brd_revision set status = 'discarded', resolved_at = now(), resolved_by = %s "
                    "where id = %s", (by, revision_id))
        cur.execute("update ingest_job set status = 'done', finished_at = coalesce(finished_at, now()), "
                    "updated_at = now() where id = (select job_id from brd_revision where id = %s) "
                    "and status = 'review'", (revision_id,))
        conn.commit()
    return {"revision_id": revision_id, "project": rev["project"], "discarded": True}

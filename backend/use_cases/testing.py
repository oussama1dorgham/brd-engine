"""Use-case TEST EXECUTION: status lifecycle + evidence (proofs) + activity timeline + cycles.

See docs/USECASE_TESTING.md. A use case is a test case; this records the RESULT of running
it, in dimensions orthogonal to the authoring lifecycle (use_case.status). None of it enters
the definition version history (use_case_versioning.py) — a definition restore never rolls
back a test result.

The status model is deliberately organized, not a flat list:
  * EXECUTION LIFECYCLE (`test_status`), mutually exclusive, one order:
        not_tested -> in_progress -> blocked -> failed -> passed
    Every value answers the same question — "what happened when we ran it".
  * NEEDS-RETEST is DERIVED, never stored as a status: a verdict pins the TEST BASIS of
    what it tested (use_case.test_basis, migration 034: the TEST-RELEVANT fields — preconditions,
    steps, expected behaviour, roles, source requirements — plus the TEXT of every cited
    requirement). When the basis moves past the pin — the case OR a requirement it cites
    changed — the verdict is stale. A move / rename / restore-to-identical never trips it.
  * VERSIONS (suites.py): a cycle runs against one published suite version. A verdict in a
    cycle pins that version's definition of the case (content_id + its basis); an ad-hoc
    verdict pins the draft. A new cycle can CARRY the passes of cases whose definition is
    unchanged since they passed — as attributed entries pointing at the result they carry.
  * SCOPE (`in_scope`) is a SEPARATE axis: an out-of-scope card is excluded from coverage
    math — it is not a verdict. Toggled via an auditable 'scope' timeline event.
  * CYCLES: a named run with a planned membership. A verdict recorded in a cycle carries
    `cycle_id`, so each cycle has its own per-case status + report (traceability), while
    `use_case.test_status` stays the latest verdict overall.

Evidence is STAGED: a proof uploaded before the verdict is PENDING (result_id NULL) and is
claimed by the next verdict/note event — so evidence lands on the verdict it proves. Proofs
on the LATEST event stay removable; proofs on past events are locked.

A re-verdict by the same tester, in the same cycle, within AMEND_WINDOW_SECONDS AMENDS the
latest verdict in place (a mis-click is corrected, not logged twice) and is undoable.

`use_case.test_status / tested_hash / in_scope` are a denormalized cache of the timeline,
recomputed by `resync_card` after every write (and after a definition restore), so the cache
can never drift from the source of truth. Every write locks the card row (FOR UPDATE).

All functions are owner-scoped and open their own short-lived pool connections, except the
`*_in(cur, …)` / `resync_card` / `purge_project` helpers, which run in the caller's tx.
"""
from __future__ import annotations

import logging
import re
import threading
import uuid
from urllib.parse import quote, urlparse

from backend.use_cases import audit, suites
from backend.core.db import pool

# Execution lifecycle, in canonical order (used everywhere — no re-ordering elsewhere).
STATUSES = ("not_tested", "in_progress", "blocked", "failed", "passed")
_DEFAULT_STATUS = "not_tested"
# A recorded verdict/attempt that can go stale when the definition moves past it.
_STALE_ELIGIBLE = ("blocked", "failed", "passed")
# Verdicts that may carry a defect reference.
_DEFECT_STATUSES = ("blocked", "failed")

PROOF_KINDS = ("image", "video", "file", "link")
MAX_PROOF_BYTES = 5 * 1024 * 1024   # 5 MB cap on a stored blob
AMEND_WINDOW_SECONDS = 300           # re-verdict within this window amends; undo allowed within it
MAX_NOTE = 5000
MAX_CAPTION = 300
MAX_DEFECT = 500
MAX_URL = 2048
MAX_BULK = 500
CYCLE_SOURCES = ("all", "not_passed", "failed_blocked", "retest", "empty")

# Served inline only for types a browser renders passively; everything else downloads.
_INLINE_MIMES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/avif",
    "video/mp4", "video/webm", "video/ogg", "video/quicktime",
    "application/pdf", "text/plain",
})
_MIME_RE = re.compile(r"[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*")


log = logging.getLogger("brd.testing")
PROOF_PREFIX = "use-case-proofs/"


def _put_blob(owner_id: int, content: bytes, mime: str) -> str | None:
    """Store a proof file in the object store → its key; None when the store is unreachable
    (the caller then keeps the bytes in Postgres, so an upload never fails on storage)."""
    from backend.core import storage
    key = f"{PROOF_PREFIX}{int(owner_id)}/{uuid.uuid4().hex}"
    try:
        storage.store().put(key, content, mime)
        return key
    except Exception:  # noqa: BLE001
        log.warning("proof: object store unavailable — keeping the file in the database", exc_info=True)
        return None


def _drop_blobs(keys: list[str]) -> None:
    """Delete objects AFTER the DB delete committed (best effort; gc_proof_objects catches misses)."""
    keys = [k for k in keys if k]
    if keys:
        from backend.core import storage
        storage.delete_quietly(keys)


def move_blobs_to_storage(batch: int = 50) -> int:
    """Move proof files still held in Postgres (older uploads, or ones stored while the object
    store was down) into the store. Idempotent; a row is switched only after its object is written.
    Returns how many moved."""
    from backend.core import storage
    moved = 0
    while True:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select id, owner_id, content, mime from use_case_test_proof "
                        "where content is not null and object_key is null order by id limit %s", (batch,))
            rows = cur.fetchall()
        if not rows:
            return moved
        for pid, owner_id, content, mime in rows:
            key = f"{PROOF_PREFIX}{int(owner_id)}/{uuid.uuid4().hex}"
            try:
                storage.store().put(key, bytes(content), clean_mime(mime))
            except Exception:  # noqa: BLE001 — store down: try again next time
                log.warning("proof: could not move blob %s to the object store", pid, exc_info=True)
                return moved
            with pool().connection() as conn, conn.cursor() as cur:
                cur.execute("update use_case_test_proof set object_key = %s, content = null "
                            "where id = %s and object_key is null", (key, pid))
                done = cur.rowcount
                conn.commit()
            if done:
                moved += 1
            else:
                _drop_blobs([key])                       # someone else moved / deleted it meanwhile


def gc_proof_objects() -> int:
    """Delete proof objects no row references (e.g. rows removed by a user-account cascade)."""
    from backend.core import storage
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select object_key from use_case_test_proof where object_key is not null")
        referenced = {r[0] for r in cur.fetchall()}
    return storage.gc_orphans(referenced, prefix=PROOF_PREFIX)


def start_blob_maintenance() -> None:
    """At startup, in the background: move remaining DB blobs to the store, then sweep orphans."""
    def run():
        try:
            n = move_blobs_to_storage()
            g = gc_proof_objects()
            if n or g:
                log.info("proof storage: moved %d blob(s), removed %d orphan object(s)", n, g)
        except Exception:  # noqa: BLE001 — never block startup on storage housekeeping
            log.warning("proof storage maintenance failed", exc_info=True)
    threading.Thread(target=run, daemon=True, name="proof-blobs").start()


class TestingError(Exception):
    """A caller error (bad status/kind, oversized blob, locked past event, closed cycle)."""


# --- pure helpers (unit-tested) ---------------------------------------------

def clean_mime(mime: str | None) -> str:
    """Normalize a client-declared Content-Type; anything malformed becomes octet-stream."""
    m = (mime or "").split(";")[0].strip().lower()
    return m if _MIME_RE.fullmatch(m) else "application/octet-stream"


def kind_for_mime(mime: str) -> str:
    """Derive the proof kind SERVER-SIDE (never trust a client-declared kind). SVG is a
    document that can carry script, so it is a plain file, not an image."""
    if mime.startswith("image/") and mime != "image/svg+xml":
        return "image"
    if mime.startswith("video/"):
        return "video"
    return "file"


def inline_ok(mime: str) -> bool:
    return mime in _INLINE_MIMES


def clean_filename(name: str | None) -> str | None:
    """Strip any path, control chars and quotes; cap the length. None if nothing is left."""
    n = re.split(r"[\\/]", name or "")[-1]
    n = re.sub(r'[\x00-\x1f\x7f"]', "", n).strip()[:200]
    return n or None


def content_disposition(filename: str | None, inline: bool) -> str:
    """RFC 6266/5987 header: an ASCII fallback plus the exact UTF-8 name (no header injection)."""
    name = filename or "proof"
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", name) or "proof"
    return f"{'inline' if inline else 'attachment'}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def valid_link(url: str | None) -> str:
    """Accept only absolute http(s) URLs (blocks javascript:/data: links in the UI)."""
    u = (url or "").strip()
    if not u:
        raise TestingError("a link proof needs a url")
    if len(u) > MAX_URL or re.search(r"[\s\x00-\x1f\x7f]", u):
        raise TestingError("invalid url")
    p = urlparse(u)
    if p.scheme.lower() not in ("http", "https") or not p.netloc:
        raise TestingError("only http(s) links are allowed")
    return u


def is_stale(status: str | None, tested_hash: str | None, def_hash: str | None) -> bool:
    """A verdict is stale when the test-relevant definition moved past the one it pinned."""
    return bool(status in _STALE_ELIGIBLE and tested_hash and def_hash and tested_hash != def_hash)


def _clip(s: str | None, n: int) -> str | None:
    s = (s or "").strip()
    return s[:n] if s else None


def _norm_status(status: str | None) -> str:
    s = (status or "").strip().lower()
    if s not in STATUSES:
        raise TestingError(f"invalid status {s!r}")
    return s


# --- helpers (in a caller-provided cursor) ----------------------------------

_CARD_COLS = "id, uid, project, test_basis, test_status, in_scope"


def _lock_card(cur, owner_id: int, *, uc_pk: int | None = None, uid: int | None = None) -> dict | None:
    """Lock + read a live card (by row PK or uid), owner-scoped. Serializes concurrent writes
    to the same card so the denormalized cache and ordinals can't race."""
    col, val = ("id", uc_pk) if uc_pk is not None else ("uid", uid)
    cur.execute(f"select {_CARD_COLS} from use_case where {col} = %s and owner_id = %s for update",
                (val, owner_id))
    r = cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "uid": r[1], "project": r[2], "basis": r[3], "test_status": r[4], "in_scope": r[5]}


def _current_version(cur, uid: int) -> int:
    cur.execute("select coalesce(max(version_no), 0) from use_case_version where use_case_uid = %s", (uid,))
    return cur.fetchone()[0]


def _latest(cur, uid: int, owner_id: int) -> dict | None:
    """The newest timeline event for a card (any kind), with its age for the amend/undo window."""
    cur.execute(
        "select id, event_kind, status, tested_by, cycle_id, note, defect_ref, amended_from, "
        "extract(epoch from now() - coalesce(amended_at, tested_at)) "
        "from use_case_test_result where use_case_uid = %s and owner_id = %s "
        "order by tested_at desc, id desc limit 1",
        (uid, owner_id),
    )
    r = cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "event_kind": r[1], "status": r[2], "tested_by": r[3], "cycle_id": r[4],
            "note": r[5], "defect_ref": r[6], "amended_from": r[7], "age": float(r[8] or 0)}


def resync_card(cur, uid: int, owner_id: int) -> None:
    """Recompute the denormalized cache (test_status, tested_hash, in_scope) from the timeline.
    The single place the cache is written — called after every event write/undo and after a
    definition restore (which re-inserts a deleted card with defaults)."""
    latest_status = ("select {col} from use_case_test_result where use_case_uid = %s and owner_id = %s "
                     "and event_kind = '{kind}' order by tested_at desc, id desc limit 1")
    cur.execute(
        "update use_case set "
        f"test_status = coalesce(({latest_status.format(col='status', kind='status')}), %s), "
        f"tested_hash = ({latest_status.format(col='case when status = any(%s) then tested_against_hash end', kind='status')}), "
        f"in_scope = coalesce(({latest_status.format(col='in_scope', kind='scope')}), true) "
        "where uid = %s and owner_id = %s",
        (uid, owner_id, _DEFAULT_STATUS, list(_STALE_ELIGIBLE), uid, owner_id, uid, owner_id, uid, owner_id),
    )


def _proof_dict(r) -> dict:
    return {"id": r[0], "result_id": r[1], "kind": r[2], "filename": r[3], "mime": r[4], "size": r[5],
            "url": r[6], "caption": r[7], "ordinal": r[8], "uploaded_at": r[9].isoformat()}


_PROOF_COLS = "id, result_id, kind, filename, mime, size, url, caption, ordinal, uploaded_at"


def _proofs_for(cur, result_ids: list[int]) -> dict[int, list[dict]]:
    """Proof metadata (no blob) grouped by result_id, ordered."""
    if not result_ids:
        return {}
    cur.execute(f"select {_PROOF_COLS} from use_case_test_proof where result_id = any(%s) "
                "order by result_id, ordinal, id", (result_ids,))
    out: dict[int, list[dict]] = {}
    for r in cur.fetchall():
        out.setdefault(r[1], []).append(_proof_dict(r))
    return out


def _pending_for(cur, uid: int, owner_id: int) -> list[dict]:
    cur.execute(f"select {_PROOF_COLS} from use_case_test_proof where use_case_uid = %s and owner_id = %s "
                "and result_id is null order by ordinal, id", (uid, owner_id))
    return [_proof_dict(r) for r in cur.fetchall()]


_EVENT_SELECT = (
    "select r.id, r.event_kind, r.status, r.note, r.in_scope, r.tested_against_ver, r.tested_at, u.email, "
    "r.defect_ref, r.cycle_id, c.name, r.amended_from, r.amended_at, r.tested_against_hash, "
    "extract(epoch from now() - coalesce(r.amended_at, r.tested_at)), r.carried_from "
    "from use_case_test_result r left join app_user u on u.id = r.tested_by "
    "left join test_cycle c on c.id = r.cycle_id "
)


def _events(cur, owner_id: int, uid: int, def_hash: str | None, *, only_id: int | None = None) -> list[dict]:
    """Timeline events (newest first) with their proofs. `undoable` marks the latest event
    while it is inside the amend/undo window; `stale` marks a verdict the definition moved past."""
    where = "where r.use_case_uid = %s and r.owner_id = %s"
    params: list = [uid, owner_id]
    cur.execute(_EVENT_SELECT + where + " order by r.tested_at desc, r.id desc", params)
    rows = cur.fetchall()
    latest_id = rows[0][0] if rows else None
    if only_id is not None:
        rows = [r for r in rows if r[0] == only_id]
    proofs = _proofs_for(cur, [r[0] for r in rows])
    return [{
        "result_id": r[0], "event_kind": r[1], "status": r[2], "note": r[3], "in_scope": r[4],
        "tested_against_ver": r[5], "tested_at": r[6].isoformat(), "tested_by": r[7],
        "defect_ref": r[8], "cycle_id": r[9], "cycle_name": r[10], "amended_from": r[11],
        "amended_at": r[12].isoformat() if r[12] else None,
        "stale": r[1] == "status" and is_stale(r[2], r[13], def_hash),
        "latest": r[0] == latest_id,
        "undoable": r[0] == latest_id and float(r[14] or 0) <= AMEND_WINDOW_SECONDS,
        "proofs": proofs.get(r[0], []),
        "carried_from": r[15],
    } for r in rows]


def _version_basis(cur, cycle_id: int, uid: int) -> tuple[str | None, int | None, int | None]:
    """(basis, content_id, version number) of `uid` in the version a cycle runs; basis None when
    the cycle predates versioning or the case isn't in that version (number still set then)."""
    cur.execute("select v.number, k.id, use_case_content_basis(k.def_hash, k.req_hashes) from test_cycle c "
                "left join use_case_suite_version v on v.id = c.suite_version_id "
                "left join use_case_suite_member m on m.suite_version_id = c.suite_version_id and m.use_case_uid = %s "
                "left join use_case_content k on k.id = m.content_id where c.id = %s", (uid, cycle_id))
    r = cur.fetchone()
    return (r[2], r[1], r[0]) if r else (None, None, None)


def _cycle_state(cur, cycle_id: int, uid: int, def_hash: str | None) -> dict:
    vbasis, _kid, vnum = _version_basis(cur, cycle_id, uid)
    cur.execute(
        "select status, tested_against_hash from use_case_test_result where cycle_id = %s and use_case_uid = %s "
        "and event_kind = 'status' order by tested_at desc, id desc limit 1", (cycle_id, uid))
    r = cur.fetchone()
    cur.execute("select 1 from test_cycle_case where cycle_id = %s and use_case_uid = %s", (cycle_id, uid))
    member = cur.fetchone() is not None
    return {"cycle_id": cycle_id, "in_cycle": member, "status": r[0] if r else _DEFAULT_STATUS,
            "needs_retest": bool(r and is_stale(r[0], r[1], vbasis or def_hash)),
            "version": vnum, "in_version": vbasis is not None or vnum is None,
            "draft_differs": bool(vbasis and def_hash and vbasis != def_hash)}


def _card_payload(cur, owner_id: int, uid: int, cycle_id: int | None = None) -> dict | None:
    """The card's post-write state, returned by every write so the UI patches without a refetch."""
    cur.execute("select id, uid, test_status, in_scope, tested_hash, test_basis from use_case "
                "where uid = %s and owner_id = %s", (uid, owner_id))
    r = cur.fetchone()
    if not r:
        return None
    out = {"id": r[0], "uid": r[1], "test_status": r[2], "in_scope": r[3],
           "needs_retest": is_stale(r[2], r[4], r[5])}
    # who touched it last (the audit entry this write just appended) — "last changed by" in the UI
    cur.execute("select actor_email, created_at, summary from use_case_audit where use_case_uid = %s "
                "and owner_id = %s order by created_at desc, id desc limit 1", (uid, owner_id))
    la = cur.fetchone()
    out.update({"last_by": la[0] if la else None, "last_at": la[1].isoformat() if la else None,
                "last_summary": la[2] if la else None})
    if cycle_id is not None:
        out["cycle"] = _cycle_state(cur, cycle_id, uid, r[5])
    return out


def _check_cycle(cur, owner_id: int, project: str, cycle_id: int) -> None:
    cur.execute("select project, status from test_cycle where id = %s and owner_id = %s", (cycle_id, owner_id))
    r = cur.fetchone()
    if not r or r[0] != project:
        raise TestingError("test cycle not found")
    if r[1] != "open":
        raise TestingError("this test cycle is closed — reopen it to record results")


def _claim_pending(cur, uid: int, owner_id: int, result_id: int) -> int:
    cur.execute("update use_case_test_proof set result_id = %s "
                "where use_case_uid = %s and owner_id = %s and result_id is null", (result_id, uid, owner_id))
    return cur.rowcount


def _status_before(cur, card: dict, cycle_id: int | None) -> str:
    """The verdict a new one replaces: the card's latest in that cycle, or overall."""
    if cycle_id is None:
        return card["test_status"]
    cur.execute("select status from use_case_test_result where cycle_id = %s and use_case_uid = %s "
                "and event_kind = 'status' order by tested_at desc, id desc limit 1", (cycle_id, card["uid"]))
    r = cur.fetchone()
    return r[0] if r else _DEFAULT_STATUS


def _pin(cur, owner_id: int, card: dict, cycle_id: int | None) -> tuple[str, int | None]:
    """What a verdict TESTS → (basis to pin, version content row). In a cycle: the cycle's version
    of the case — refused if the case isn't in that version (e.g. added to the draft after it was
    published). Ad hoc: the draft, linked to the latest version's content only when that version
    holds exactly this definition."""
    if cycle_id is not None:
        vbasis, kid, vnum = _version_basis(cur, cycle_id, card["uid"])
        if vnum is not None:
            if vbasis is None:
                raise TestingError(f"This case isn't in v{vnum}, the version this cycle tests — publish a new "
                                   "version and start a new cycle to test it.")
            return vbasis, kid
    cur.execute("select k.id from use_case_suite_version v "
                "join use_case_suite_member m on m.suite_version_id = v.id and m.use_case_uid = %s "
                "join use_case_content k on k.id = m.content_id "
                "where v.owner_id = %s and v.project = %s "
                "and use_case_content_basis(k.def_hash, k.req_hashes) = %s order by v.number desc limit 1",
                (card["uid"], owner_id, card["project"], card["basis"]))
    r = cur.fetchone()
    return card["basis"], (r[0] if r else None)


def _record_in(cur, owner_id: int, card: dict, *, event_kind: str, status: str | None, note: str | None,
               defect_ref: str | None, cycle_id: int | None, by: int, allow_amend: bool = True,
               claim: bool = True, audit_summary: str | None = None, audit_source: str = "user",
               audit_detail: dict | None = None) -> dict:
    """Write one status/note event for a LOCKED card (caller's tx), and its audit entry.
    Returns {result_id, amended, claimed}."""
    uid, project = card["uid"], card["project"]
    pin, content_id = _pin(cur, owner_id, card, cycle_id)
    ver = _current_version(cur, uid)
    if event_kind == "status" and status not in _DEFECT_STATUSES:
        defect_ref = None
    before = _status_before(cur, card, cycle_id) if event_kind == "status" else None
    latest = _latest(cur, uid, owner_id) if (allow_amend and event_kind == "status") else None
    if (latest and latest["event_kind"] == "status" and latest["tested_by"] == by
            and latest["cycle_id"] == cycle_id and latest["age"] <= AMEND_WINDOW_SECONDS):
        # Amend: a quick re-verdict corrects the last one instead of logging a second entry.
        result_id = latest["id"]
        keep_defect = defect_ref if defect_ref is not None else (
            latest["defect_ref"] if status in _DEFECT_STATUSES else None)
        cur.execute(
            "update use_case_test_result set "
            "amended_from = case when status <> %s then status else amended_from end, "
            "status = %s, note = coalesce(%s, note), defect_ref = %s, tested_against_hash = %s, "
            "content_id = %s, carried_from = null, tested_against_ver = %s, amended_at = now() where id = %s",
            (status, status, note, keep_defect, pin, content_id, ver, result_id),
        )
        amended = True
    else:
        cur.execute(
            "insert into use_case_test_result (use_case_uid, owner_id, project, event_kind, status, note, "
            "defect_ref, cycle_id, tested_against_ver, tested_against_hash, tested_by, content_id) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
            (uid, owner_id, project, event_kind, status if event_kind == "status" else card["test_status"],
             note, defect_ref, cycle_id, ver, pin, by, content_id),
        )
        result_id = cur.fetchone()[0]
        amended = False
    claimed = _claim_pending(cur, uid, owner_id, result_id) if claim else 0
    if cycle_id is not None:
        cur.execute("insert into test_cycle_case (cycle_id, use_case_uid, planned_hash) values (%s,%s,%s) "
                    "on conflict do nothing", (cycle_id, uid, pin))
        if cur.rowcount:
            audit.log(cur, uid=uid, owner_id=owner_id, project=project, actor_id=by, category="cycle",
                      action="cycle_planned", summary="Planned into the cycle by recording a result",
                      cycle_id=cycle_id)
    resync_card(cur, uid, owner_id)
    # --- audit ---
    extra = {"note": note, "defect_ref": defect_ref, "attachments": claimed or None, **(audit_detail or {})}
    if event_kind == "note":
        files = f"{claimed} attachment{'s' if claimed > 1 else ''}"
        if audit_summary:
            summary = audit_summary
        elif note and claimed:
            summary = f"Added a note with {files}"
        elif note or defect_ref:
            summary = "Added a note"
        else:
            summary = f"Logged evidence ({files})"
        audit.log(cur, uid=uid, owner_id=owner_id, project=project, actor_id=by, category="test",
                  action="note_added", summary=summary, cycle_id=cycle_id, result_id=result_id,
                  detail=extra, source=audit_source)
    elif amended:
        prev = latest["status"]
        summary = (f"Changed verdict {audit.label(prev)} → {audit.label(status)}" if prev != status
                   else f"Updated the {audit.label(status)} verdict")
        audit.log(cur, uid=uid, owner_id=owner_id, project=project, actor_id=by, category="test",
                  action="status_amended", summary=summary, status_before=prev, status_after=status,
                  cycle_id=cycle_id, result_id=result_id, detail=extra, source=audit_source)
    else:
        audit.log(cur, uid=uid, owner_id=owner_id, project=project, actor_id=by, category="test",
                  action="status_recorded", summary=audit_summary or f"Marked {audit.label(status)}",
                  status_before=before, status_after=status, cycle_id=cycle_id, result_id=result_id,
                  detail=extra, source=audit_source)
    return {"result_id": result_id, "amended": amended, "claimed": claimed}


# --- write: status / notes / scope ------------------------------------------

def record(owner_id: int, uc_pk: int, *, event_kind: str = "status", status: str | None = None,
           note: str | None = None, defect_ref: str | None = None, cycle_id: int | None = None,
           by: int | None = None) -> dict | None:
    """Record a verdict ('status') or a log entry ('note') on a card. Staged evidence is
    claimed by the event; a cycle-scoped verdict joins the card to that cycle. A same-tester
    re-verdict inside the amend window amends in place. Returns {event, card, amended,
    claimed}, or None if the card isn't found. Raises TestingError on a caller error."""
    event_kind = "note" if event_kind == "note" else "status"
    status = _norm_status(status) if event_kind == "status" else None
    note, defect_ref = _clip(note, MAX_NOTE), _clip(defect_ref, MAX_DEFECT)
    by = by if by is not None else owner_id
    with pool().connection() as conn, conn.cursor() as cur:
        card = _lock_card(cur, owner_id, uc_pk=uc_pk)
        if not card:
            return None
        if cycle_id is not None:
            _check_cycle(cur, owner_id, card["project"], cycle_id)
        if event_kind == "note" and not note and not defect_ref and not _pending_for(cur, card["uid"], owner_id):
            raise TestingError("nothing to log — add a note or attach evidence")
        res = _record_in(cur, owner_id, card, event_kind=event_kind, status=status, note=note,
                         defect_ref=defect_ref, cycle_id=cycle_id, by=by)
        event = _events(cur, owner_id, card["uid"], card["basis"], only_id=res["result_id"])
        payload = _card_payload(cur, owner_id, card["uid"], cycle_id)
        conn.commit()
    return {"event": event[0] if event else None, "card": payload,
            "amended": res["amended"], "claimed": res["claimed"]}


def record_bulk(owner_id: int, uc_pks: list[int], status: str, *, note: str | None = None,
                cycle_id: int | None = None, by: int | None = None) -> list[dict]:
    """One verdict on many cards, atomically (one transaction). No amend, and staged evidence
    stays with its own card — bulk is for triage, evidence is per-card. Returns card payloads."""
    status = _norm_status(status)
    note = _clip(note, MAX_NOTE)
    by = by if by is not None else owner_id
    ids = list(dict.fromkeys(int(i) for i in uc_pks))[:MAX_BULK]
    out: list[dict] = []
    with pool().connection() as conn, conn.cursor() as cur:
        for pk in ids:
            card = _lock_card(cur, owner_id, uc_pk=pk)
            if not card:
                continue
            if cycle_id is not None:
                _check_cycle(cur, owner_id, card["project"], cycle_id)
            _record_in(cur, owner_id, card, event_kind="status", status=status, note=note, defect_ref=None,
                       cycle_id=cycle_id, by=by, allow_amend=False, claim=False,
                       audit_summary=f"Marked {audit.label(status)} (bulk, {len(ids)} cases)",
                       audit_detail={"bulk": len(ids)})
            out.append(_card_payload(cur, owner_id, card["uid"], cycle_id))
        conn.commit()
    return out


def _scope_in(cur, owner_id: int, card: dict, in_scope: bool, note: str | None, by: int) -> int:
    cur.execute(
        "insert into use_case_test_result (use_case_uid, owner_id, project, event_kind, status, "
        "note, in_scope, tested_against_ver, tested_against_hash, tested_by) "
        "values (%s,%s,%s,'scope',%s,%s,%s,%s,%s,%s) returning id",
        (card["uid"], owner_id, card["project"], card["test_status"], note, bool(in_scope),
         _current_version(cur, card["uid"]), card["basis"], by),
    )
    rid = cur.fetchone()[0]
    resync_card(cur, card["uid"], owner_id)
    audit.log(cur, uid=card["uid"], owner_id=owner_id, project=card["project"], actor_id=by, category="scope",
              action="scope_in" if in_scope else "scope_out",
              summary="Returned to scope" if in_scope else "Marked out of scope — excluded from coverage",
              result_id=rid, detail={"note": note})
    return rid


def set_scope(owner_id: int, uc_pk: int, in_scope: bool, *, note: str | None = None,
              cycle_id: int | None = None, by: int | None = None) -> dict | None:
    """Toggle the SCOPE axis (in/out of scope) — independent of the verdict. Records an
    auditable 'scope' event. Returns {event, card} or None if not found."""
    by = by if by is not None else owner_id
    with pool().connection() as conn, conn.cursor() as cur:
        card = _lock_card(cur, owner_id, uc_pk=uc_pk)
        if not card:
            return None
        rid = _scope_in(cur, owner_id, card, in_scope, _clip(note, MAX_NOTE), by)
        event = _events(cur, owner_id, card["uid"], card["basis"], only_id=rid)
        payload = _card_payload(cur, owner_id, card["uid"], cycle_id)
        conn.commit()
    return {"event": event[0] if event else None, "card": payload}


def set_scope_bulk(owner_id: int, uc_pks: list[int], in_scope: bool, *, by: int | None = None) -> list[dict]:
    by = by if by is not None else owner_id
    out: list[dict] = []
    with pool().connection() as conn, conn.cursor() as cur:
        for pk in list(dict.fromkeys(int(i) for i in uc_pks))[:MAX_BULK]:
            card = _lock_card(cur, owner_id, uc_pk=pk)
            if not card or card["in_scope"] == bool(in_scope):
                continue
            _scope_in(cur, owner_id, card, in_scope, None, by)
            out.append(_card_payload(cur, owner_id, card["uid"]))
        conn.commit()
    return out


def undo(owner_id: int, result_id: int, *, cycle_id: int | None = None, by: int | None = None) -> dict | None:
    """Undo the LATEST event inside the window: an amended verdict reverts to what it replaced;
    a fresh event is removed and its evidence returns to the staging area (and a note's text
    is handed back so it can be re-used). Returns {card, restored_note, reverted} or None."""
    by = by if by is not None else owner_id
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select use_case_uid from use_case_test_result where id = %s and owner_id = %s",
                    (result_id, owner_id))
        r = cur.fetchone()
        if not r:
            return None
        card = _lock_card(cur, owner_id, uid=r[0])
        if not card:
            raise TestingError("this use case no longer exists")
        latest = _latest(cur, card["uid"], owner_id)
        if not latest or latest["id"] != result_id:
            raise TestingError("only the latest entry can be undone")
        if latest["age"] > AMEND_WINDOW_SECONDS or latest["tested_by"] != by:
            raise TestingError("the undo window for this entry has passed")
        restored_note = None
        if latest["amended_from"]:
            cur.execute("update use_case_test_result set status = %s, amended_from = null, amended_at = null, "
                        "defect_ref = case when %s = any(%s) then defect_ref end where id = %s",
                        (latest["amended_from"], latest["amended_from"], list(_DEFECT_STATUSES), result_id))
            reverted = True
        else:
            cur.execute("update use_case_test_proof set result_id = null where result_id = %s", (result_id,))
            cur.execute("delete from use_case_test_result where id = %s", (result_id,))
            restored_note = latest["note"]
            reverted = False
        resync_card(cur, card["uid"], owner_id)
        # audit: the entry is gone/reverted from the timeline, but its undo is recorded for good
        cur.execute("select test_status from use_case where uid = %s and owner_id = %s", (card["uid"], owner_id))
        overall = cur.fetchone()[0]
        now_status = _status_before(cur, {**card, "test_status": overall}, latest["cycle_id"])
        if reverted:
            audit.log(cur, uid=card["uid"], owner_id=owner_id, project=card["project"], actor_id=by,
                      category="test", action="status_reverted",
                      summary=f"Undid the change {audit.label(latest['amended_from'])} → {audit.label(latest['status'])}",
                      status_before=latest["status"], status_after=latest["amended_from"],
                      cycle_id=latest["cycle_id"], result_id=result_id)
        else:
            what = {"status": f"Marked {audit.label(latest['status'])}", "note": "a note",
                    "scope": "a scope change"}.get(latest["event_kind"], "an entry")
            is_status = latest["event_kind"] == "status"
            audit.log(cur, uid=card["uid"], owner_id=owner_id, project=card["project"], actor_id=by,
                      category="scope" if latest["event_kind"] == "scope" else "test", action="entry_undone",
                      summary=f"Undid {what}", status_before=latest["status"] if is_status else None,
                      status_after=now_status if is_status else None, cycle_id=latest["cycle_id"],
                      result_id=result_id, detail={"note": latest["note"]})
        payload = _card_payload(cur, owner_id, card["uid"], cycle_id)
        conn.commit()
    return {"card": payload, "restored_note": restored_note, "reverted": reverted}


def edit_event(owner_id: int, result_id: int, *, note: str | None = None,
               defect_ref: str | None = None, by: int | None = None) -> dict | None:
    """Correct the note / defect of the LATEST event in place. Older events are immutable."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select use_case_uid from use_case_test_result where id = %s and owner_id = %s",
                    (result_id, owner_id))
        r = cur.fetchone()
        if not r:
            return None
        card = _lock_card(cur, owner_id, uid=r[0])
        latest = _latest(cur, r[0], owner_id) if card else None
        if not latest or latest["id"] != result_id:
            raise TestingError("only the latest entry can be edited")
        sets, vals = [], []
        if note is not None:
            sets.append("note = %s"); vals.append(_clip(note, MAX_NOTE))
        if defect_ref is not None:
            sets.append("defect_ref = %s"); vals.append(_clip(defect_ref, MAX_DEFECT))
        if sets:
            cur.execute(f"update use_case_test_result set {', '.join(sets)} where id = %s", vals + [result_id])
            audit.log(cur, uid=card["uid"], owner_id=owner_id, project=card["project"],
                      actor_id=by if by is not None else owner_id, category="test", action="entry_edited",
                      summary="Edited the latest entry's " + " and ".join(
                          (["note"] if note is not None else []) + (["defect"] if defect_ref is not None else [])),
                      cycle_id=latest["cycle_id"], result_id=result_id,
                      detail={"note_before": latest["note"], "note": _clip(note, MAX_NOTE) if note is not None else None,
                              "defect_before": latest["defect_ref"],
                              "defect_ref": _clip(defect_ref, MAX_DEFECT) if defect_ref is not None else None})
        event = _events(cur, owner_id, card["uid"], card["basis"], only_id=result_id)
        conn.commit()
    return {"event": event[0] if event else None}


# --- write: proofs ----------------------------------------------------------

def stage_proof(owner_id: int, uc_pk: int, *, filename: str | None = None, mime: str | None = None,
                content: bytes | None = None, url: str | None = None, caption: str | None = None,
                by: int | None = None) -> dict | None:
    """Stage a proof on a card (PENDING — claimed by the next verdict/note). A `url` makes a
    link proof (http(s) only); otherwise `content` is a file whose kind is derived from its
    sanitized mime. Returns the proof metadata, or None if the card is not found."""
    if url is not None:
        kind, url, content, mime, filename = "link", valid_link(url), None, None, None
    else:
        if not content:
            raise TestingError("a file proof needs content")
        if len(content) > MAX_PROOF_BYTES:
            raise TestingError(f"file too large (max {MAX_PROOF_BYTES // (1024 * 1024)} MB)")
        mime = clean_mime(mime)
        kind = kind_for_mime(mime)
        filename = clean_filename(filename)
    caption = _clip(caption, MAX_CAPTION)
    by = by if by is not None else owner_id
    size = len(content) if content else None
    key = _put_blob(owner_id, content, mime) if content else None     # object first, then the row
    with pool().connection() as conn, conn.cursor() as cur:
        card = _lock_card(cur, owner_id, uc_pk=uc_pk)
        if not card:
            _drop_blobs([key])
            return None
        cur.execute("select coalesce(max(ordinal), -1) + 1 from use_case_test_proof "
                    "where use_case_uid = %s and result_id is null", (card["uid"],))
        ordinal = cur.fetchone()[0]
        cur.execute(
            "insert into use_case_test_proof (result_id, use_case_uid, owner_id, project, kind, filename, mime, "
            f"size, content, object_key, url, caption, ordinal, uploaded_by) "
            f"values (null,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning {_PROOF_COLS}",
            (card["uid"], owner_id, card["project"], kind, filename, mime, size,
             None if key else content, key, url, caption, ordinal, by),
        )
        proof = _proof_dict(cur.fetchone())
        audit.log(cur, uid=card["uid"], owner_id=owner_id, project=card["project"], actor_id=by,
                  category="evidence", action="proof_added",
                  summary=f"Attached {filename or url or kind}" + (f" ({kind})" if kind != "file" and filename else ""),
                  detail={"proof_id": proof["id"], "kind": kind, "size": proof["size"], "staged": True})
        conn.commit()
    return proof


def delete_proof(owner_id: int, proof_id: int, by: int | None = None) -> bool:
    """Delete a proof — a staged one always; a bound one only while its event is the latest."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select result_id, use_case_uid, coalesce(filename, url, kind), kind, project, object_key "
                    "from use_case_test_proof where id = %s and owner_id = %s", (proof_id, owner_id))
        r = cur.fetchone()
        if not r:
            return False
        result_id, uid, name, kind, project, key = r
        if result_id is not None:
            _lock_card(cur, owner_id, uid=uid)
            latest = _latest(cur, uid, owner_id)
            if not latest or latest["id"] != result_id:
                raise TestingError("this evidence belongs to a past result and is locked")
        cur.execute("delete from use_case_test_proof where id = %s and owner_id = %s", (proof_id, owner_id))
        ok = cur.rowcount > 0
        if ok:
            audit.log(cur, uid=uid, owner_id=owner_id, project=project, actor_id=by if by is not None else owner_id,
                      category="evidence", action="proof_removed",
                      summary=f"Removed {name}" + (" (staged)" if result_id is None else " from a recorded result"),
                      result_id=result_id, detail={"proof_id": proof_id, "kind": kind})
        conn.commit()
    if ok:
        _drop_blobs([key])
    return ok


def set_caption(owner_id: int, proof_id: int, caption: str | None, by: int | None = None) -> bool:
    """Relabel a proof (allowed on any event — a caption is not evidence tampering; it is audited)."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update use_case_test_proof set caption = %s where id = %s and owner_id = %s "
                    "returning use_case_uid, project, coalesce(filename, url, kind)",
                    (_clip(caption, MAX_CAPTION), proof_id, owner_id))
        r = cur.fetchone()
        if r:
            audit.log(cur, uid=r[0], owner_id=owner_id, project=r[1], actor_id=by if by is not None else owner_id,
                      category="evidence", action="proof_relabelled", summary=f"Relabelled {r[2]}",
                      detail={"proof_id": proof_id, "caption": _clip(caption, MAX_CAPTION)})
        conn.commit()
    return bool(r)


def get_proof_blob(owner_id: int, proof_id: int) -> tuple[bytes, str, str] | None:
    """(content, mime, filename) for a file proof, owner-scoped. None if missing or a link."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select content, mime, filename, object_key from use_case_test_proof "
                    "where id = %s and owner_id = %s", (proof_id, owner_id))
        r = cur.fetchone()
    if not r:
        return None
    if r[3]:
        from backend.core import storage
        try:
            return (storage.store().get(r[3]), clean_mime(r[1]), r[2] or "proof")
        except Exception:  # noqa: BLE001
            log.warning("proof %s: object %s unavailable", proof_id, r[3], exc_info=True)
            return None
    if r[0] is None:
        return None
    return (bytes(r[0]), clean_mime(r[1]), r[2] or "proof")


# --- reads ------------------------------------------------------------------

def timeline(owner_id: int, uid: int, cycle_id: int | None = None) -> dict:
    """Full activity timeline (newest first) + staged evidence + the card's current state
    (and, with a cycle, its state within that cycle)."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select test_basis from use_case where uid = %s and owner_id = %s", (uid, owner_id))
        live = cur.fetchone()
        def_hash = live[0] if live else None
        events = _events(cur, owner_id, uid, def_hash)
        pending = _pending_for(cur, uid, owner_id)
        card = _card_payload(cur, owner_id, uid, cycle_id)
        current_ver = _current_version(cur, uid)
    return {"uid": uid, "live": live is not None, "card": card, "current_version": current_ver,
            "amend_window_seconds": AMEND_WINDOW_SECONDS, "pending": pending, "events": events}


# --- cycles -----------------------------------------------------------------

def _cycle_members_status(cur, cycle_id: int) -> list[tuple]:
    """(uid, live?, in_scope, status, stale, tested_at, defect_ref, draft_differs, carried) per
    planned member. Staleness is measured against the definition the CYCLE tests — its version's
    (the draft's for a cycle from before versioning); `draft_differs` flags a case whose draft
    changed after that version (the tester must follow the version's definition)."""
    cur.execute(
        "select m.use_case_uid, uc.id is not null, coalesce(uc.in_scope, true), s.status, "
        "  (s.status = any(%s) and s.tested_against_hash is distinct from coalesce(vb.basis, uc.test_basis)), "
        "  s.tested_at, s.defect_ref, (vb.basis is not null and uc.test_basis is distinct from vb.basis), "
        "  coalesce(s.carried, false) "
        "from test_cycle_case m join test_cycle cy on cy.id = m.cycle_id "
        "left join use_case uc on uc.uid = m.use_case_uid "
        "left join lateral (select use_case_content_basis(k.def_hash, k.req_hashes) as basis "
        "  from use_case_suite_member sm join use_case_content k on k.id = sm.content_id "
        "  where sm.suite_version_id = cy.suite_version_id and sm.use_case_uid = m.use_case_uid) vb on true "
        "left join lateral (select status, tested_against_hash, tested_at, defect_ref, carried_from is not null as carried "
        "  from use_case_test_result r "
        "  where r.cycle_id = m.cycle_id and r.use_case_uid = m.use_case_uid and r.event_kind = 'status' "
        "  order by r.tested_at desc, r.id desc limit 1) s on true "
        "where m.cycle_id = %s",
        (list(_STALE_ELIGIBLE), cycle_id),
    )
    return cur.fetchall()


def _cycle_stats(rows: list[tuple]) -> dict:
    by_status: dict[str, int] = {}
    total = executed = retest = oos = carried = 0
    for _uid, live, in_scope, status, stale, _at, _d, _differs, was_carried in rows:
        if not live:
            continue
        if not in_scope:
            oos += 1
            continue
        s = status or _DEFAULT_STATUS
        total += 1
        by_status[s] = by_status.get(s, 0) + 1
        executed += s != _DEFAULT_STATUS
        retest += bool(stale)
        carried += bool(was_carried)
    return {"total": total, "executed": executed, "needs_retest": retest, "out_of_scope": oos,
            "carried": carried, "by_status": by_status}


def _cycle_dict(r) -> dict:
    """A cycle + the version it tests; `outdated` when a newer version has been published since
    (the run can still be finished — a new cycle tests the new version)."""
    return {"id": r[0], "name": r[1], "description": r[2], "status": r[3],
            "created_at": r[4].isoformat(), "closed_at": r[5].isoformat() if r[5] else None,
            "version": {"id": r[6], "number": r[7], "label": r[8]} if r[6] else None,
            "latest_version": r[9], "outdated": bool(r[6] and r[9] and r[9] > r[7])}


_CYCLE_SELECT = (
    "select c.id, c.name, c.description, c.status, c.created_at, c.closed_at, v.id, v.number, v.label, "
    "(select max(x.number) from use_case_suite_version x where x.owner_id = c.owner_id and x.project = c.project) "
    "from test_cycle c left join use_case_suite_version v on v.id = c.suite_version_id "
)


def _get_cycle(cur, owner_id: int, cycle_id: int, project: str | None = None) -> dict | None:
    cur.execute(_CYCLE_SELECT + "where c.id = %s and c.owner_id = %s"
                + (" and c.project = %s" if project is not None else ""),
                (cycle_id, owner_id) + ((project,) if project is not None else ()))
    r = cur.fetchone()
    return _cycle_dict(r) if r else None


def list_cycles(owner_id: int, project: str) -> list[dict]:
    """Cycles for a project (open first, newest first), each with live progress stats."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(_CYCLE_SELECT + "where c.owner_id = %s and c.project = %s "
                    "order by (c.status = 'open') desc, c.created_at desc", (owner_id, project))
        cycles = [_cycle_dict(r) for r in cur.fetchall()]
        for c in cycles:
            c["stats"] = _cycle_stats(_cycle_members_status(cur, c["id"]))
    return cycles


def _folder_descendants(cur, owner_id: int, project: str, roots: list[int]) -> set[int]:
    cur.execute("select id, parent_id from use_case_folder where owner_id = %s and project = %s", (owner_id, project))
    kids: dict[int | None, list[int]] = {}
    for fid, pid in cur.fetchall():
        kids.setdefault(pid, []).append(fid)
    out, stack = set(), list(roots)
    while stack:
        f = stack.pop()
        if f in out:
            continue
        out.add(f)
        stack.extend(kids.get(f, []))
    return out


def _carry_passes(cur, owner_id: int, project: str, cycle: dict, members: list[tuple], by: int) -> int:
    """Seed a new cycle with the PASS of every member whose latest verdict is a pass taken on
    exactly this version's definition (same basis: the case AND its cited requirements are
    unchanged). Each becomes a new, attributed entry in the cycle pointing at the result it
    carries (whose evidence stays where it is). Anything else starts Not run."""
    n = 0
    vnum = cycle["version"]["number"]
    for uid, basis, content_id in members:
        cur.execute("select r.id, r.status, r.tested_against_hash, r.tested_at, c.name, u.email "
                    "from use_case_test_result r left join test_cycle c on c.id = r.cycle_id "
                    "left join app_user u on u.id = r.tested_by "
                    "where r.use_case_uid = %s and r.owner_id = %s and r.event_kind = 'status' "
                    "order by r.tested_at desc, r.id desc limit 1", (uid, owner_id))
        r = cur.fetchone()
        if not r or r[1] != "passed" or r[2] != basis:
            continue
        cur.execute(
            "insert into use_case_test_result (use_case_uid, owner_id, project, event_kind, status, cycle_id, "
            "tested_against_ver, tested_against_hash, tested_by, content_id, carried_from) "
            "values (%s,%s,%s,'status','passed',%s,%s,%s,%s,%s,%s) returning id",
            (uid, owner_id, project, cycle["id"], _current_version(cur, uid), basis, by, content_id, r[0]))
        rid = cur.fetchone()[0]
        resync_card(cur, uid, owner_id)
        src = f"“{r[4]}”" if r[4] else "an ad-hoc run"
        audit.log(cur, uid=uid, owner_id=owner_id, project=project, actor_id=by, category="test",
                  action="status_carried", summary=f"Carried Passed from {src} — unchanged in v{vnum}",
                  status_before=_DEFAULT_STATUS, status_after="passed", cycle_id=cycle["id"], result_id=rid,
                  suite_version_id=cycle["version"]["id"],
                  detail={"carried_from": r[0], "passed_by": r[5], "passed_at": r[3].isoformat()})
        n += 1
    return n


def create_cycle(owner_id: int, project: str, name: str, *, description: str | None = None,
                 source: str = "all", case_types: list[str] | None = None, folder_ids: list[int] | None = None,
                 from_cycle_id: int | None = None, carry: bool = True, by: int | None = None) -> dict:
    """Create a cycle and PLAN its membership:
      all            — every in-scope case (optionally narrowed by case type / folders)
      not_passed     — cases not yet passed (in `from_cycle_id`, else overall)
      failed_blocked — cases that failed or are blocked (a regression re-run)
      retest         — cases whose verdict is stale against the current definition
      empty          — start empty; cases join as you record results in it
    The cycle runs the LATEST PUBLISHED VERSION (one must exist): only cases in it are planned,
    each pinned to that version's definition; cases added to the draft since are left out
    (`unpublished_excluded`). With `carry`, passes on unchanged definitions carry over."""
    name = _clip(name, 120)
    if not name:
        raise TestingError("a test cycle needs a name")
    if source not in CYCLE_SOURCES:
        raise TestingError(f"invalid source {source!r}")
    by = by if by is not None else owner_id
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select id, number from use_case_suite_version where owner_id = %s and project = %s "
                    "order by number desc limit 1", (owner_id, project))
        ver = cur.fetchone()
        if not ver:
            raise TestingError("Publish the use cases as a version first — a test cycle runs against a "
                               "published version, so its results say exactly what was tested.")
        vid = ver[0]
        cur.execute("select m.use_case_uid, use_case_content_basis(k.def_hash, k.req_hashes), k.id "
                    "from use_case_suite_member m join use_case_content k on k.id = m.content_id "
                    "where m.suite_version_id = %s", (vid,))
        vmem = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        cur.execute(
            "select uc.uid, uc.tested_hash, uc.test_status, uc.case_type, uc.folder_id "
            "from use_case uc where uc.owner_id = %s and uc.project = %s and uc.in_scope",
            (owner_id, project),
        )
        cards = cur.fetchall()
        excluded = sum(1 for c in cards if c[0] not in vmem)
        prior: dict[int, str] | None = None
        if from_cycle_id is not None:
            cur.execute("select 1 from test_cycle where id = %s and owner_id = %s and project = %s",
                        (from_cycle_id, owner_id, project))
            if not cur.fetchone():
                raise TestingError("source cycle not found")
            prior = {r[0]: (r[3] or _DEFAULT_STATUS) for r in _cycle_members_status(cur, from_cycle_id)}
        folders = _folder_descendants(cur, owner_id, project, folder_ids) if folder_ids else None
        types = {t for t in (case_types or []) if t}
        members = []
        for uid, tested_hash, status, ctype, fid in (cards if source != "empty" else []):
            if uid not in vmem:
                continue
            vbasis, kid = vmem[uid]
            retest = status in _STALE_ELIGIBLE and tested_hash is not None and tested_hash != vbasis
            if types and ctype not in types:
                continue
            if folders is not None and fid not in folders:
                continue
            if prior is not None:
                if uid not in prior:
                    continue
                status = prior[uid]
            if source == "not_passed" and status == "passed":
                continue
            if source == "failed_blocked" and status not in _DEFECT_STATUSES:
                continue
            if source == "retest" and not retest:
                continue
            members.append((uid, vbasis, kid))
        cur.execute("insert into test_cycle (owner_id, project, name, description, created_by, suite_version_id) "
                    "values (%s,%s,%s,%s,%s,%s) returning id",
                    (owner_id, project, name, _clip(description, 1000), by, vid))
        cycle = _get_cycle(cur, owner_id, cur.fetchone()[0])
        if members:
            cur.executemany("insert into test_cycle_case (cycle_id, use_case_uid, planned_hash) values (%s,%s,%s) "
                            "on conflict do nothing", [(cycle["id"], u, h) for u, h, _k in members])
            for u, _h, _k in members:
                audit.log(cur, uid=u, owner_id=owner_id, project=project, actor_id=by, category="cycle",
                          action="cycle_planned", summary=f"Planned into new cycle “{name}” (v{cycle['version']['number']})",
                          cycle_id=cycle["id"], suite_version_id=vid, detail={"source": source})
        cycle["carried"] = _carry_passes(cur, owner_id, project, cycle, members, by) if carry and members else 0
        cycle["unpublished_excluded"] = excluded
        cycle["stats"] = _cycle_stats(_cycle_members_status(cur, cycle["id"]))
        conn.commit()
    return cycle


def update_cycle(owner_id: int, cycle_id: int, *, name: str | None = None, description: str | None = None,
                 status: str | None = None) -> dict | None:
    sets, vals = [], []
    if name is not None:
        n = _clip(name, 120)
        if not n:
            raise TestingError("a test cycle needs a name")
        sets.append("name = %s"); vals.append(n)
    if description is not None:
        sets.append("description = %s"); vals.append(_clip(description, 1000))
    if status is not None:
        if status not in ("open", "closed"):
            raise TestingError(f"invalid cycle status {status!r}")
        sets.append("status = %s"); vals.append(status)
        sets.append("closed_at = " + ("now()" if status == "closed" else "null"))
    with pool().connection() as conn, conn.cursor() as cur:
        if sets:
            cur.execute(f"update test_cycle set {', '.join(sets)} where id = %s and owner_id = %s",
                        vals + [cycle_id, owner_id])
        cycle = _get_cycle(cur, owner_id, cycle_id)
        if not cycle:
            return None
        cycle["stats"] = _cycle_stats(_cycle_members_status(cur, cycle_id))
        conn.commit()
    return cycle


def delete_cycle(owner_id: int, cycle_id: int) -> bool:
    """Delete a cycle. Its results are KEPT (cycle_id -> NULL: they become ad-hoc runs)."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("delete from test_cycle where id = %s and owner_id = %s", (cycle_id, owner_id))
        ok = cur.rowcount > 0
        conn.commit()
    return ok


def set_cycle_members(owner_id: int, cycle_id: int, *, add_uc_ids: list[int] | None = None,
                      remove_uids: list[int] | None = None, by: int | None = None) -> dict | None:
    """Plan cases into / out of a cycle. Removing a member keeps its recorded results. Only cases
    in the cycle's version can join (`not_in_version` counts the others), pinned to that version."""
    by = by if by is not None else owner_id
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select project, suite_version_id from test_cycle where id = %s and owner_id = %s",
                    (cycle_id, owner_id))
        r = cur.fetchone()
        if not r:
            return None
        project, vid = r
        skipped = 0
        if add_uc_ids:
            cur.execute("select uc.uid, coalesce(use_case_content_basis(k.def_hash, k.req_hashes), uc.test_basis), "
                        "  %s::bigint is null or k.id is not null "
                        "from use_case uc left join use_case_suite_member m on m.suite_version_id = %s "
                        "  and m.use_case_uid = uc.uid left join use_case_content k on k.id = m.content_id "
                        "where uc.id = any(%s) and uc.owner_id = %s and uc.project = %s",
                        (vid, vid, list(add_uc_ids)[:MAX_BULK], owner_id, project))
            rows = cur.fetchall()
            skipped = sum(1 for _u, _h, inv in rows if not inv)
            for u, h in [(u, h) for u, h, inv in rows if inv]:
                cur.execute("insert into test_cycle_case (cycle_id, use_case_uid, planned_hash) values (%s,%s,%s) "
                            "on conflict do nothing", (cycle_id, u, h))
                if cur.rowcount:
                    audit.log(cur, uid=u, owner_id=owner_id, project=project, actor_id=by, category="cycle",
                              action="cycle_planned", summary="Planned into the cycle", cycle_id=cycle_id)
        if remove_uids:
            cur.execute("delete from test_cycle_case where cycle_id = %s and use_case_uid = any(%s) "
                        "returning use_case_uid", (cycle_id, list(remove_uids)[:MAX_BULK]))
            for (u,) in cur.fetchall():
                audit.log(cur, uid=u, owner_id=owner_id, project=project, actor_id=by, category="cycle",
                          action="cycle_unplanned", summary="Removed from the cycle (its results are kept)",
                          cycle_id=cycle_id)
        stats = _cycle_stats(_cycle_members_status(cur, cycle_id))
        conn.commit()
    return {"cycle_id": cycle_id, "stats": stats, "not_in_version": skipped}


def cycle_statuses(owner_id: int, cycle_id: int) -> dict | None:
    """Per-member status within a cycle — overlaid on the tree while the cycle is active."""
    with pool().connection() as conn, conn.cursor() as cur:
        cycle = _get_cycle(cur, owner_id, cycle_id)
        if not cycle:
            return None
        rows = _cycle_members_status(cur, cycle_id)
    cycle["stats"] = _cycle_stats(rows)
    cases = {str(uid): {"status": status or _DEFAULT_STATUS, "needs_retest": bool(stale),
                        "tested_at": at.isoformat() if at else None, "defect_ref": defect,
                        "draft_differs": bool(differs), "carried": bool(was_carried)}
             for uid, live, _in_scope, status, stale, at, defect, differs, was_carried in rows if live}
    return {"cycle": cycle, "cases": cases}


# --- report (traceability) --------------------------------------------------

def _coverage(statuses: list[str], stale: int) -> str:
    """One word for a requirement's test state across the cases that cover it."""
    if not statuses:
        return "uncovered"
    if "failed" in statuses:
        return "failed"
    if "blocked" in statuses:
        return "blocked"
    if all(s == "passed" for s in statuses):
        return "retest" if stale else "passed"
    if all(s == _DEFAULT_STATUS for s in statuses):
        return "not_run"
    return "partial"


def _version_context(cur, owner_id: int, project: str, version: dict | None) -> dict:
    """The report's version header: the version it covers and what changed since the one
    before it (compact per-case list), so a report reads "v3 — 4 modified, 2 added since v2"."""
    if not version:
        return {"version": None, "changes_since_previous": None}
    cur.execute("select id, number from use_case_suite_version where owner_id = %s and project = %s "
                "and number < %s order by number desc limit 1", (owner_id, project, version["number"]))
    prev = cur.fetchone()
    if not prev:
        return {"version": version, "changes_since_previous": None}
    d = suites.diff_rows(suites._version_rows(cur, prev[0]), suites._version_rows(cur, version["id"]))
    return {"version": version, "changes_since_previous": {
        "base": prev[1], "summary": d["summary"],
        "items": [{k: i.get(k) for k in ("uid", "uc_id", "title", "case_type", "change", "fields", "moved", "retest")}
                  for i in d["items"] if i["change"] != "reordered"]}}


def report(owner_id: int, project: str, cycle_id: int | None = None) -> dict:
    """Coverage + traceability report — overall, or for one cycle (its planned members).
    Out-of-scope cards are excluded from every denominator and reported separately.
    Includes: status/case-type breakdown, per-case rows (for export), defects, and a
    requirement -> cases -> verdict matrix with a coverage state per requirement.

    VERSIONED: a cycle's report is built from the VERSION it tested — each case's definition,
    folder and citations as they were then, staleness against that definition, requirement
    text as of the version (flagged when it has changed or been removed since) — so it stays
    true after the draft moves on. The overall report covers the draft and the latest version.
    Both carry the version header and the changes since the previous version."""
    from backend.requirements.edit import list_requirements   # lazy: keep this module's import surface small

    cycle = None
    ver_reqs: dict[int, str] = {}                  # chunk id -> requirement text hash, as of the version
    with pool().connection() as conn, conn.cursor() as cur:
        if cycle_id is not None:
            cycle = _get_cycle(cur, owner_id, cycle_id, project)
            if not cycle:
                raise TestingError("test cycle not found")
        if cycle and cycle["version"]:
            vctx = _version_context(cur, owner_id, project, cycle["version"])
            draft = None
        else:
            vid = suites.latest(cur, owner_id, project)
            vctx = _version_context(cur, owner_id, project,
                                    suites._version_meta(cur, vid) if vid and cycle is None else None)
            draft = suites.diff_rows(suites._version_rows(cur, vid) if vid else {},
                                     suites._draft_rows(cur, owner_id, project)[0])["summary"] if cycle is None else None
        member_clause = ("and exists (select 1 from test_cycle_case m where m.cycle_id = %s "
                         "and m.use_case_uid = uc.uid) " if cycle_id is not None else "")
        cycle_clause = "and r.cycle_id = %s " if cycle_id is not None else ""
        latest_sql = ("left join lateral (select r.id, r.status, r.tested_against_hash, r.tested_at, r.tested_by, "
                      "  r.defect_ref, r.note, r.carried_from from use_case_test_result r where r.use_case_uid = {uid} "
                      "  and r.owner_id = %s and r.event_kind = 'status' " + cycle_clause +
                      "  order by r.tested_at desc, r.id desc limit 1) s on true "
                      "left join app_user u on u.id = s.tested_by ")
        tail = ("s.status, s.tested_against_hash, s.tested_at, u.email, s.defect_ref, s.note, "
                "(select count(*) from use_case_test_proof p where p.result_id = s.id), s.carried_from is not null ")
        if cycle and cycle["version"]:
            # the version's definitions (folder path in place of a folder id), plus its requirement hashes
            cur.execute(
                "select uc.id, m.use_case_uid, k.data->>'uc_id', k.data->>'title', coalesce(k.data->>'case_type', 'functional'), "
                "  sm.folder_path, coalesce(uc.in_scope, true), k.data->'source_chunk_ids', "
                "  use_case_content_basis(k.def_hash, k.req_hashes), " + tail + ", k.req_hashes "
                "from test_cycle_case m "
                "join use_case_suite_member sm on sm.suite_version_id = %s and sm.use_case_uid = m.use_case_uid "
                "join use_case_content k on k.id = sm.content_id "
                "left join use_case uc on uc.uid = m.use_case_uid "
                + latest_sql.format(uid="m.use_case_uid") +
                "where m.cycle_id = %s order by sm.ordinal, m.use_case_uid",
                (cycle["version"]["id"], owner_id, cycle_id, cycle_id),
            )
            rows = []
            for r in cur.fetchall():
                ver_reqs.update({int(k): h for k, h in (r[-1] or {}).items() if h})
                rows.append(r[:-1])
            texts = suites._requirement_texts(cur, owner_id, project, set(ver_reqs.values()), {})
        else:
            cur.execute(
                "select uc.id, uc.uid, uc.uc_id, uc.title, uc.case_type, uc.folder_id, uc.in_scope, "
                "  uc.source_chunk_ids, uc.test_basis, " + tail +
                "from use_case uc " + latest_sql.format(uid="uc.uid") +
                "where uc.owner_id = %s and uc.project = %s " + member_clause +
                "order by uc.folder_id, uc.ordinal, uc.id",
                # lateral's owner (+ cycle) params come first in text order, then owner/project, then membership
                [owner_id] + ([cycle_id] if cycle_id is not None else []) + [owner_id, project] +
                ([cycle_id] if cycle_id is not None else []),
            )
            rows = cur.fetchall()
            texts = {}
        cur.execute("select id, parent_id, name from use_case_folder where owner_id = %s and project = %s",
                    (owner_id, project))
        fmeta = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    def fpath(fid):
        if isinstance(fid, list):                  # a version's folder path (names, root → leaf)
            return " / ".join(fid)
        parts, seen = [], set()
        while fid is not None and fid in fmeta and fid not in seen:
            seen.add(fid)
            parts.append(fmeta[fid][1])
            fid = fmeta[fid][0]
        return " / ".join(reversed(parts))

    cases, defects = [], []
    by_status: dict[str, int] = {}
    by_type: dict[str, dict[str, int]] = {}
    out_of_scope = needs_retest = executed = 0
    for (pk, uid, uc_id, title, ctype, fid, in_scope, cids, def_hash, status, t_hash, t_at, t_by,
         defect, note, nproofs, carried) in rows:
        if not in_scope:
            out_of_scope += 1
            continue
        st = status or _DEFAULT_STATUS
        stale = is_stale(st, t_hash, def_hash)
        by_status[st] = by_status.get(st, 0) + 1
        by_type.setdefault(ctype, {})[st] = by_type.get(ctype, {}).get(st, 0) + 1
        executed += st != _DEFAULT_STATUS
        needs_retest += stale
        row = {"id": pk, "uid": uid, "uc_id": uc_id, "title": title, "case_type": ctype, "folder": fpath(fid),
               "status": st, "needs_retest": stale, "tested_at": t_at.isoformat() if t_at else None,
               "tested_by": t_by, "defect_ref": defect, "note": note, "proofs": int(nproofs or 0),
               "source_chunk_ids": list(cids or []), "carried": bool(carried), "live": pk is not None}
        cases.append(row)
        if st in _DEFECT_STATUSES or defect:
            defects.append({k: row[k] for k in ("id", "uid", "uc_id", "title", "status", "defect_ref",
                                                 "note", "tested_at", "needs_retest")})

    by_chunk: dict[int, list[dict]] = {}
    for c in cases:
        for cid in c["source_chunk_ids"]:
            by_chunk.setdefault(int(cid), []).append(c)
    requirements, cov_counts = [], {}
    live_reqs = list_requirements(owner_id, project)
    if ver_reqs:
        # requirement text as the version had it; flag what changed or disappeared since
        from backend.use_cases.incremental import req_hash
        live_ids = set()
        for rq in live_reqs:
            live_ids.add(int(rq["chunk_id"]))
            h = ver_reqs.get(int(rq["chunk_id"]))
            if h and h != req_hash(rq.get("text")) and texts.get(h) is not None:
                rq["text"], rq["changed_since_version"] = texts[h], True
        live_reqs = live_reqs + [{"chunk_id": c, "req_id": None, "section": None, "ordinal": None,
                                  "text": texts.get(h) or "", "removed_since_version": True}
                                 for c, h in sorted(ver_reqs.items()) if c not in live_ids]
    for rq in live_reqs:
        linked = by_chunk.get(int(rq["chunk_id"]), [])
        sts = [c["status"] for c in linked]
        cov = _coverage(sts, sum(c["needs_retest"] for c in linked))
        cov_counts[cov] = cov_counts.get(cov, 0) + 1
        counts: dict[str, int] = {}
        for s in sts:
            counts[s] = counts.get(s, 0) + 1
        requirements.append({"chunk_id": rq["chunk_id"], "req_id": rq.get("req_id"), "section": rq.get("section"),
                             "ordinal": rq.get("ordinal"), "text": (rq.get("text") or "")[:240],
                             "coverage": cov, "counts": counts, "case_uids": [c["uid"] for c in linked],
                             "changed_since_version": bool(rq.get("changed_since_version")),
                             "removed_since_version": bool(rq.get("removed_since_version"))})
    total = len(cases)
    passed = by_status.get("passed", 0)
    return {"project": project, "cycle": cycle, "total": total, "executed": executed,
            "out_of_scope": out_of_scope, "needs_retest": needs_retest, "by_status": by_status,
            "by_case_type": by_type, "pass_rate": (passed / executed) if executed else None,
            "progress": (executed / total) if total else None, "coverage": cov_counts,
            "requirements": requirements, "cases": cases, "defects": defects,
            "carried": sum(c["carried"] for c in cases), **vctx, "draft_changes": draft}


# --- run-over-run comparison (regressions) -------------------------------------

RUN_CHANGES = ("regressed", "new_failure", "still_failing", "fixed", "newly_passing", "not_rerun",
               "dropped", "not_run", "still_passing")


def classify_run_change(a: str | None, b: str | None) -> str:
    """How one case moved from run A to run B (None = not planned in that run). `regressed` —
    passed in A, failing now — is the signal a release manager looks for first."""
    fail_a, fail_b = a in _DEFECT_STATUSES, b in _DEFECT_STATUSES
    if b is None:
        return "dropped"
    if fail_b:
        return "regressed" if a == "passed" else "still_failing" if fail_a else "new_failure"
    if b == "passed":
        return "fixed" if fail_a else "still_passing" if a == "passed" else "newly_passing"
    return "not_rerun" if (a == "passed" or fail_a) else "not_run"


def compare_cycles(owner_id: int, project: str, base_id: int, target_id: int) -> dict:
    """Run-over-run view: every case of either run, its verdict in each, how it moved, and —
    when both runs are versioned — whether its definition changed between their versions. A
    case failing on an UNCHANGED definition points at the product; one failing after its
    definition (or a cited requirement) changed may just need its test updated."""
    with pool().connection() as conn, conn.cursor() as cur:
        a = _get_cycle(cur, owner_id, base_id, project)
        b = _get_cycle(cur, owner_id, target_id, project)
        if not a or not b:
            raise TestingError("test cycle not found")
        ra = {r[0]: r for r in _cycle_members_status(cur, base_id)}
        rb = {r[0]: r for r in _cycle_members_status(cur, target_id)}
        va = suites._version_rows(cur, a["version"]["id"]) if a["version"] else {}
        vb = suites._version_rows(cur, b["version"]["id"]) if b["version"] else {}
        defs = suites.diff_rows(va, vb) if (a["version"] and b["version"]) else None
        cur.execute("select uid, id, uc_id, title, case_type from use_case where owner_id = %s and project = %s "
                    "and uid = any(%s)", (owner_id, project, list(set(ra) | set(rb))))
        live = {r[0]: r for r in cur.fetchall()}
    change = {i["uid"]: i for i in defs["items"]} if defs else {}
    summary = {k: 0 for k in RUN_CHANGES}
    items = []
    for uid in sorted(set(ra) | set(rb)):
        sa = (ra[uid][3] or _DEFAULT_STATUS) if uid in ra else None
        sb = (rb[uid][3] or _DEFAULT_STATUS) if uid in rb else None
        kind = classify_run_change(sa, sb)
        summary[kind] += 1
        meta = (vb.get(uid) or va.get(uid) or {}).get("data") or {}
        lv = live.get(uid)
        ch = change.get(uid)
        items.append({
            "uid": uid, "id": lv[1] if lv else None,
            "uc_id": meta.get("uc_id") or (lv[2] if lv else None), "title": meta.get("title") or (lv[3] if lv else None),
            "case_type": meta.get("case_type") or (lv[4] if lv else None),
            "base_status": sa, "target_status": sb, "change": kind,
            "carried": bool(uid in rb and rb[uid][8]), "defect_ref": rb[uid][6] if uid in rb else None,
            "definition": ch["change"] if ch else ("unchanged" if defs is not None else None),
            "definition_fields": ch["fields"] if ch else [], "retest": bool(ch and ch["retest"]),
        })
    return {"base": a, "target": b, "summary": summary, "items": items,
            "definition_changes": defs["summary"] if defs else None}


# --- lifecycle --------------------------------------------------------------

def purge_project(cur, owner_id: int, project: str) -> list[str]:
    """Hard-delete every test artifact of a project (BRD deletion) — in the caller's tx.
    Test tables have no FK to use_case, so without this their blobs would orphan forever.
    Returns the proof object keys for the caller to delete AFTER it commits."""
    cur.execute("select object_key from use_case_test_proof where owner_id = %s and object_key is not null "
                "and (project = %s or result_id in (select id from use_case_test_result "
                "where owner_id = %s and project = %s))", (owner_id, project, owner_id, project))
    keys = [r[0] for r in cur.fetchall()]
    cur.execute("delete from use_case_test_proof where owner_id = %s and (project = %s or result_id in "
                "(select id from use_case_test_result where owner_id = %s and project = %s))",
                (owner_id, project, owner_id, project))
    cur.execute("delete from use_case_test_result where owner_id = %s and project = %s", (owner_id, project))
    cur.execute("delete from test_cycle where owner_id = %s and project = %s", (owner_id, project))
    suites.purge_project(cur, owner_id, project)      # after results/cycles: they RESTRICT versions
    audit.purge_project(cur, owner_id, project)
    return keys

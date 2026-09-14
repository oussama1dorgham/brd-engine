"""Per-use-case version history + audit log + restore (see docs/USECASE_VERSIONING.md).

The core invariant: no use case is deleted or overwritten without first recording an
immutable version row IN THE SAME TRANSACTION. That one rule delivers version history,
the audit trail ("what changed"), a restorable backup, and non-destructive regeneration.

Each version is a FULL snapshot of a card's state (`data`), keyed by the card's stable
`use_case_uid` (not the row PK), so a card's history is one continuous thread even across
a delete -> restore. Because owner_id/project/uc_id are denormalized and use_case_id is
ON DELETE SET NULL, a deleted card's versions remain — that is the backup, and what powers
the Trash / restore-deleted view.

The `_record`/`_capture_row` helpers take a live cursor and run inside the CALLER's
transaction (use_cases.py write paths), so a version and its mutation commit atomically.
The public read/restore functions open their own connection.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

from .db import pool

# Snapshot fields compared for the audit summary + diff (folder_path included: a move
# is an auditable change too). roles/steps/source_chunk_ids are lists.
_FIELDS = ("title", "description", "roles", "preconditions", "steps",
           "expected_behaviour", "source_chunk_ids", "status", "folder_path")

_LABELS = {
    "title": "title", "description": "description", "roles": "roles",
    "preconditions": "preconditions", "steps": "steps",
    "expected_behaviour": "expected behaviour", "source_chunk_ids": "source requirements",
    "status": "status", "folder_path": "folder",
}


# --- capture / record (in the caller's transaction) -------------------------

def _folder_path(cur, folder_id) -> str:
    """"Scope / Sub-theme" (or just "Scope") for a folder id; '' if unknown."""
    if folder_id is None:
        return ""
    cur.execute(
        "select coalesce(pf.name || ' / ', '') || f.name "
        "from use_case_folder f left join use_case_folder pf on pf.id = f.parent_id "
        "where f.id = %s",
        (folder_id,),
    )
    r = cur.fetchone()
    return r[0] if r else ""


def _capture_row(cur, uc_pk: int) -> dict | None:
    """Full snapshot of one live use case (by row PK). None if it doesn't exist.
    Returns {uid, uc_id, owner_id, project, use_case_id, data}."""
    cur.execute(
        "select uc.uid, uc.uc_id, uc.owner_id, uc.project, uc.folder_id, uc.title, "
        "uc.description, uc.roles, uc.preconditions, uc.steps, uc.expected_behaviour, "
        "uc.source_chunk_ids, uc.status, uc.batch_hash "
        "from use_case uc where uc.id = %s",
        (uc_pk,),
    )
    r = cur.fetchone()
    if not r:
        return None
    (uid, uc_id, owner_id, project, folder_id, title, description, roles, preconditions,
     steps, expected_behaviour, source_chunk_ids, status, batch_hash) = r
    data = {
        "title": title, "description": description, "roles": roles or [],
        "preconditions": preconditions, "steps": steps or [],
        "expected_behaviour": expected_behaviour, "source_chunk_ids": source_chunk_ids or [],
        "status": status, "batch_hash": batch_hash,
        "folder_path": _folder_path(cur, folder_id),
    }
    return {"uid": uid, "uc_id": uc_id, "owner_id": owner_id, "project": project,
            "use_case_id": uc_pk, "data": data}


def _norm_val(v):
    return v if v is not None else ""


def _summary(cur, uid: int, kind: str, new_data: dict) -> str:
    """One-line audit label. For create/delete/regenerate it's fixed; for edit/move/restore
    it diffs against the latest recorded version and names the changed fields."""
    if kind == "create":
        return "Created"
    if kind == "delete":
        return "Deleted"
    if kind == "regenerate":
        return "Superseded by regeneration"
    if kind == "move":
        return f"Moved to {new_data.get('folder_path') or '—'}"
    cur.execute("select data from use_case_version where use_case_uid = %s "
                "order by version_no desc limit 1", (uid,))
    prev = cur.fetchone()
    if not prev:
        return "Restored" if kind == "restore" else "Updated"
    old = prev[0] or {}
    changed = [_LABELS[f] for f in _FIELDS if _norm_val(old.get(f)) != _norm_val(new_data.get(f))]
    if kind == "restore":
        return "Restored" + (f" ({', '.join(changed)} changed)" if changed else " (no change)")
    return (", ".join(changed) + " changed") if changed else "No change"


def _record(cur, *, uid: int, use_case_id: int | None, owner_id: int, project: str,
            uc_id: str | None, kind: str, data: dict, changed_by: int | None,
            summary: str | None = None) -> int:
    """Append a version row for a card (in the caller's tx). Returns the new version_no."""
    if summary is None:
        summary = _summary(cur, uid, kind, data)
    cur.execute("select coalesce(max(version_no), 0) + 1 from use_case_version where use_case_uid = %s",
                (uid,))
    vno = cur.fetchone()[0]
    cur.execute(
        "insert into use_case_version (use_case_uid, use_case_id, owner_id, project, uc_id, "
        "version_no, change_kind, change_summary, changed_by, data) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (uid, use_case_id, owner_id, project, uc_id, vno, kind, summary, changed_by, Jsonb(data)),
    )
    return vno


def record_change(cur, uc_pk: int, kind: str, owner_id: int, changed_by: int | None = None) -> None:
    """Capture the current state of a live card (by row PK) and record it as `kind`.
    No-op if the card is missing or not owned by `owner_id` (a defensive guard so a
    version is never written for a mutation that didn't apply). Runs in the caller's tx."""
    cap = _capture_row(cur, uc_pk)
    if not cap or cap["owner_id"] != owner_id:
        return
    _record(cur, uid=cap["uid"], use_case_id=cap["use_case_id"], owner_id=owner_id,
            project=cap["project"], uc_id=cap["uc_id"], kind=kind, data=cap["data"],
            changed_by=changed_by if changed_by is not None else owner_id)


# --- public reads -----------------------------------------------------------

def history(owner_id: int, uid: int) -> dict:
    """Version timeline for a card (newest first), plus whether a live row still exists."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select 1 from use_case where uid = %s and owner_id = %s", (uid, owner_id))
        live = cur.fetchone() is not None
        cur.execute(
            "select v.version_no, v.change_kind, v.change_summary, v.changed_at, u.email, v.uc_id "
            "from use_case_version v left join app_user u on u.id = v.changed_by "
            "where v.use_case_uid = %s and v.owner_id = %s order by v.version_no desc",
            (uid, owner_id),
        )
        versions = [{"version_no": r[0], "change_kind": r[1], "change_summary": r[2],
                     "changed_at": r[3].isoformat(), "changed_by": r[4], "uc_id": r[5]}
                    for r in cur.fetchall()]
    return {"uid": uid, "live": live, "versions": versions}


def diff(owner_id: int, uid: int, version_no: int) -> dict:
    """Field-level before→after for one version vs. its predecessor (audit 'what changed')."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select data from use_case_version where use_case_uid=%s and owner_id=%s "
                    "and version_no=%s", (uid, owner_id, version_no))
        cur_row = cur.fetchone()
        if not cur_row:
            return {"uid": uid, "version_no": version_no, "fields": {}, "found": False}
        new = cur_row[0] or {}
        cur.execute("select version_no, data from use_case_version where use_case_uid=%s and owner_id=%s "
                    "and version_no < %s order by version_no desc limit 1", (uid, owner_id, version_no))
        prev = cur.fetchone()
    old = (prev[1] or {}) if prev else {}
    fields = {f: {"before": old.get(f), "after": new.get(f)}
              for f in _FIELDS if _norm_val(old.get(f)) != _norm_val(new.get(f))}
    return {"uid": uid, "version_no": version_no,
            "prev_version_no": prev[0] if prev else None, "fields": fields,
            "snapshot": new, "found": True}


def list_deleted(owner_id: int, project: str) -> list[dict]:
    """Cards with history but no live row (deleted or superseded) — the Trash / backup view.
    Shows the latest recorded version of each so it can be restored."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "select distinct on (v.use_case_uid) v.use_case_uid, v.uc_id, v.version_no, "
            "       v.change_kind, v.changed_at, v.data->>'title' "
            "from use_case_version v "
            "where v.owner_id=%s and v.project=%s "
            "  and not exists (select 1 from use_case uc where uc.uid = v.use_case_uid) "
            "order by v.use_case_uid, v.version_no desc",
            (owner_id, project),
        )
        return [{"uid": r[0], "uc_id": r[1], "version_no": r[2], "change_kind": r[3],
                 "changed_at": r[4].isoformat(), "title": r[5]} for r in cur.fetchall()]


# --- restore ----------------------------------------------------------------

def restore(owner_id: int, uid: int, version_no: int, changed_by: int | None = None) -> dict:
    """Restore a card to a prior version. Overwrites the live row from the snapshot, or
    re-inserts the card (same uid) if it had been deleted, resolving the folder path
    (creating folders as needed). The restore is itself recorded as a new version, so it
    is undoable and the pre-restore state is never lost (it's already the latest version)."""
    from .use_cases import _folder   # lazy: avoids a circular import

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select data, uc_id, project from use_case_version "
                    "where use_case_uid=%s and owner_id=%s and version_no=%s",
                    (uid, owner_id, version_no))
        row = cur.fetchone()
        if not row:
            return {"restored": False, "reason": "no such version"}
        data, uc_id, project = row
        data = data or {}

        # resolve the snapshot's folder path (scope [/ sub-theme]) to a folder id
        parts = [p.strip() for p in (data.get("folder_path") or "").split(" / ") if p.strip()]
        fid = None
        parent = None
        for name in parts:
            fid = _folder(cur, owner_id, project, parent, name)
            parent = fid

        cur.execute("select id from use_case where uid=%s and owner_id=%s", (uid, owner_id))
        live = cur.fetchone()
        if live:
            uc_pk = live[0]
            cur.execute(
                "update use_case set folder_id=%s, title=%s, description=%s, roles=%s, "
                "preconditions=%s, steps=%s, expected_behaviour=%s, source_chunk_ids=%s, "
                "status=%s, batch_hash=%s, updated_at=now() where id=%s and owner_id=%s",
                (fid, data.get("title", ""), data.get("description", ""), Jsonb(data.get("roles", [])),
                 data.get("preconditions", ""), Jsonb(data.get("steps", [])),
                 data.get("expected_behaviour", ""), Jsonb(data.get("source_chunk_ids", [])),
                 data.get("status", "draft"), data.get("batch_hash"), uc_pk, owner_id),
            )
        else:   # deleted card → re-insert with its ORIGINAL uid + label
            cur.execute("select coalesce(max(ordinal), -1) + 1 from use_case where folder_id=%s", (fid,))
            ordinal = cur.fetchone()[0]
            cur.execute(
                "insert into use_case (uid, owner_id, project, folder_id, uc_id, title, description, "
                "roles, preconditions, steps, expected_behaviour, source_chunk_ids, ordinal, status, "
                "batch_hash) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
                (uid, owner_id, project, fid, uc_id, data.get("title", ""), data.get("description", ""),
                 Jsonb(data.get("roles", [])), data.get("preconditions", ""), Jsonb(data.get("steps", [])),
                 data.get("expected_behaviour", ""), Jsonb(data.get("source_chunk_ids", [])), ordinal,
                 data.get("status", "draft"), data.get("batch_hash")),
            )
            uc_pk = cur.fetchone()[0]

        restored_data = dict(data)
        restored_data["folder_path"] = _folder_path(cur, fid)
        _record(cur, uid=uid, use_case_id=uc_pk, owner_id=owner_id, project=project, uc_id=uc_id,
                kind="restore", data=restored_data,
                changed_by=changed_by if changed_by is not None else owner_id,
                summary=f"Restored from v{version_no}")
        conn.commit()
    return {"restored": True, "uid": uid, "from_version": version_no, "use_case_id": uc_pk}

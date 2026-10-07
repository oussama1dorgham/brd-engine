"""Version-controlled use-case suites (migration 032).

The live use_case rows are the one editable DRAFT of a BRD's suite; `publish` freezes it into an
immutable, numbered SUITE VERSION. Runs and results bind to a version, so a report always says
exactly what was tested. Versions are built by ONE SQL function (use_case_snapshot) and the draft
is read by ONE SQL function (use_case_draft_rows) — the same code made the v1 baseline — so the
diff a user reviews is exactly what publish freezes. Publishing an unchanged draft is a no-op.
"""
import hashlib
import json

from backend.core.db import pool
from backend.use_cases import audit

# what changed for one case between two sides (old → new), most significant first
CHANGES = ("added", "removed", "modified", "requirement_changed", "moved", "reordered", "unchanged")
_FIELDS = ("title", "description", "case_type", "roles", "preconditions", "steps", "expected_behaviour",
           "source_chunk_ids")


class SuiteError(Exception):
    pass


class Stale(SuiteError):
    """The draft changed after the user reviewed it — re-review before publishing."""


def _lock(cur, owner_id: int, project: str) -> None:
    # same key as use_case_snapshot(): serializes publish / review-check per BRD
    cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"use_case_suite:{owner_id}:{project}",))


def _row(uid, ordinal, folder_path, def_hash, data, content_hash, req_hashes, basis_hash) -> dict:
    return {"uid": uid, "ordinal": ordinal, "folder_path": folder_path or [], "def_hash": def_hash,
            "data": data or {}, "content_hash": content_hash, "req_hashes": req_hashes or {},
            "basis_hash": basis_hash}


def _draft_rows(cur, owner_id: int, project: str) -> tuple[dict, dict]:
    """{uid: row} of the draft + {hash: text} of the requirement texts it cites."""
    cur.execute("select uid, ordinal, folder_path, def_hash, data, content_hash, req_hashes, basis_hash, reqs "
                "from use_case_draft_rows(%s, %s)", (owner_id, project))
    rows, texts = {}, {}
    for r in cur.fetchall():
        rows[r[0]] = _row(*r[:8])
        for q in r[8] or []:
            texts[q["hash"]] = q["text"]
    return rows, texts


def _version_rows(cur, version_id: int) -> dict:
    cur.execute("select m.use_case_uid, m.ordinal, m.folder_path, k.def_hash, k.data, k.content_hash, "
                "       k.req_hashes, k.basis_hash "
                "from use_case_suite_member m join use_case_content k on k.id = m.content_id "
                "where m.suite_version_id = %s", (version_id,))
    return {r[0]: _row(*r) for r in cur.fetchall()}


def fingerprint(rows: dict) -> str:
    """Identity of a draft's publishable state — what publish's staleness check compares."""
    key = sorted((u, r["basis_hash"], json.dumps(r["folder_path"], ensure_ascii=False), r["ordinal"])
                 for u, r in rows.items())
    return hashlib.md5(json.dumps(key, ensure_ascii=False).encode()).hexdigest()


def diff_rows(old: dict, new: dict) -> dict:
    """Pure: compare two {uid: row} sides. Every case gets exactly one `change` (the most
    significant); `moved` is also flagged on a modified case, and `retest` says whether an
    earlier verdict stops being valid (test-relevant fields or a cited requirement changed)."""
    items, summary = [], {c: 0 for c in CHANGES}
    summary["retest"] = 0
    for uid in sorted(set(old) | set(new)):
        a, b = old.get(uid), new.get(uid)
        cur = b or a
        item = {"uid": uid, "uc_id": cur["data"].get("uc_id"), "title": cur["data"].get("title"),
                "case_type": cur["data"].get("case_type"), "fields": [], "requirements": [],
                "moved": False, "retest": False}
        if a is None:
            item["change"] = "added"
        elif b is None:
            item["change"] = "removed"
        else:
            item["moved"] = a["folder_path"] != b["folder_path"]
            item["fields"] = [f for f in _FIELDS if a["data"].get(f) != b["data"].get(f)]
            item["requirements"] = sorted(k for k in set(a["req_hashes"]) | set(b["req_hashes"])
                                          if a["req_hashes"].get(k) != b["req_hashes"].get(k))
            item["retest"] = a["def_hash"] != b["def_hash"] or bool(item["requirements"])
            if a["content_hash"] != b["content_hash"]:
                item["change"] = "modified"
            elif a["basis_hash"] != b["basis_hash"]:
                item["change"] = "requirement_changed"
            elif item["moved"]:
                item["change"] = "moved"
            elif a["ordinal"] != b["ordinal"]:
                item["change"] = "reordered"
            else:
                item["change"] = "unchanged"
            if item["change"] in ("modified", "requirement_changed"):
                item["before"], item["after"] = a["data"], b["data"]
            if item["moved"]:
                item["folder_before"], item["folder_after"] = a["folder_path"], b["folder_path"]
        summary[item["change"]] += 1
        summary["retest"] += item["retest"]
        items.append(item)
    summary["has_changes"] = any(summary[c] for c in CHANGES if c != "unchanged")
    return {"summary": summary, "items": [i for i in items if i["change"] != "unchanged"]}


def _version_meta(cur, version_id: int) -> dict | None:
    cur.execute("select v.id, v.number, v.status, v.source, v.label, v.brd_revision_no, v.counts, u.email, "
                "       v.created_at, (select count(*) from test_cycle c where c.suite_version_id = v.id) "
                "from use_case_suite_version v left join app_user u on u.id = v.created_by where v.id = %s",
                (version_id,))
    r = cur.fetchone()
    return r and {"id": r[0], "number": r[1], "status": r[2], "source": r[3], "label": r[4],
                  "brd_revision_no": r[5], "counts": r[6], "created_by": r[7],
                  "created_at": r[8].isoformat(), "runs": r[9]}


def _own_version(cur, owner_id: int, project: str, version_id: int) -> None:
    cur.execute("select 1 from use_case_suite_version where id = %s and owner_id = %s and project = %s",
                (version_id, owner_id, project))
    if not cur.fetchone():
        raise SuiteError("version not found")


def _requirement_texts(cur, owner_id: int, project: str, hashes: set, draft_texts: dict) -> dict:
    found = {h: draft_texts[h] for h in hashes if h in draft_texts}
    missing = [h for h in hashes if h not in found]
    if missing:
        cur.execute("select hash, text from use_case_req_text where owner_id = %s and project = %s "
                    "and hash = any(%s)", (owner_id, project, missing))
        found.update(dict(cur.fetchall()))
    return found


def diff(owner_id: int, project: str, base_id: int | None, target: int | str = "draft") -> dict:
    """Compare version `base_id` (None = empty) with version `target` or the draft. Changed
    requirement citations come with their text on both sides."""
    with pool().connection() as conn, conn.cursor() as cur:
        if base_id is not None:
            _own_version(cur, owner_id, project, base_id)
        old = _version_rows(cur, base_id) if base_id is not None else {}
        texts = {}
        if target == "draft":
            new, texts = _draft_rows(cur, owner_id, project)
        else:
            _own_version(cur, owner_id, project, int(target))
            new = _version_rows(cur, int(target))
        out = diff_rows(old, new)
        need = set()
        for i in out["items"]:
            for k in i["requirements"]:
                need.update(h for h in (old.get(i["uid"], {}).get("req_hashes", {}).get(k),
                                        new.get(i["uid"], {}).get("req_hashes", {}).get(k)) if h)
        text_of = _requirement_texts(cur, owner_id, project, need, texts)
        for i in out["items"]:
            i["requirements"] = [{
                "chunk_id": int(k),
                "before": text_of.get(old.get(i["uid"], {}).get("req_hashes", {}).get(k)),
                "after": text_of.get(new.get(i["uid"], {}).get("req_hashes", {}).get(k)),
            } for k in i["requirements"]]
        out["base"] = _version_meta(cur, base_id) if base_id is not None else None
        out["target"] = "draft" if target == "draft" else _version_meta(cur, int(target))
        if target == "draft":
            out["fingerprint"] = fingerprint(new)
    return out


def case_in_version(owner_id: int, project: str, version_id: int, uid: int) -> dict:
    """One case as `version_id` defines it (what a cycle on that version tests), with the text
    of every requirement it cites as of that version."""
    with pool().connection() as conn, conn.cursor() as cur:
        _own_version(cur, owner_id, project, version_id)
        cur.execute("select v.number, k.data, k.req_hashes, m.folder_path from use_case_suite_member m "
                    "join use_case_suite_version v on v.id = m.suite_version_id "
                    "join use_case_content k on k.id = m.content_id "
                    "where m.suite_version_id = %s and m.use_case_uid = %s", (version_id, uid))
        r = cur.fetchone()
        if not r:
            raise SuiteError("this case is not in that version")
        number, data, req_hashes, folder = r
        texts = _requirement_texts(cur, owner_id, project, {h for h in (req_hashes or {}).values() if h}, {})
    return {"version": number, "uid": uid, "case": data, "folder_path": folder,
            "requirements": [{"chunk_id": int(k), "text": texts.get(h)} for k, h in (req_hashes or {}).items()]}


def latest(cur, owner_id: int, project: str) -> int | None:
    cur.execute("select id from use_case_suite_version where owner_id = %s and project = %s "
                "order by number desc limit 1", (owner_id, project))
    row = cur.fetchone()
    return row[0] if row else None


def status(owner_id: int, project: str) -> dict:
    """The version bar: latest version + what the draft changes against it (counts only)."""
    with pool().connection() as conn, conn.cursor() as cur:
        vid = latest(cur, owner_id, project)
        new, _ = _draft_rows(cur, owner_id, project)
        d = diff_rows(_version_rows(cur, vid) if vid else {}, new)
        cur.execute("select revision_no from brd_document where owner_id = %s and project = %s "
                    "order by id desc limit 1", (owner_id, project))
        rev = cur.fetchone()
        return {"latest": _version_meta(cur, vid) if vid else None, "draft": d["summary"],
                "fingerprint": fingerprint(new), "brd_revision_no": rev[0] if rev else None}


def publish(owner_id: int, project: str, by: int | None, label: str | None = None,
            expect: str | None = None, source: str = "publish") -> dict:
    """Freeze the draft as the next version — atomically, and only if it is still exactly what
    the user reviewed (`expect` = the fingerprint the review showed). Every changed case gets an
    audit entry naming the version. An unchanged draft publishes nothing."""
    label = (label or "").strip()[:200] or None
    with pool().connection() as conn, conn.cursor() as cur:
        _lock(cur, owner_id, project)
        base = latest(cur, owner_id, project)
        new, _ = _draft_rows(cur, owner_id, project)
        if not new:
            raise SuiteError("There are no use cases to publish.")
        if expect is not None and fingerprint(new) != expect:
            raise Stale("The use cases changed since you reviewed them — review the changes again.")
        changes = diff_rows(_version_rows(cur, base) if base else {}, new)
        if base and not changes["summary"]["has_changes"]:
            meta = _version_meta(cur, base)
            conn.rollback()
            return {"created": False, "version": meta}
        cur.execute("select use_case_snapshot(%s, %s, %s, %s, %s)", (owner_id, project, by, source, label))
        vid = cur.fetchone()[0]
        meta = _version_meta(cur, vid)
        for i in changes["items"]:
            if i["change"] == "reordered":
                continue
            what = i["change"].replace("_", " ")
            if i["fields"]:
                what += f" ({', '.join(f.replace('_', ' ') for f in i['fields'])})"
            if i["moved"] and i["change"] != "moved":
                what += ", moved"
            audit.log(cur, uid=i["uid"], owner_id=owner_id, project=project, actor_id=by,
                      category="version", action=f"version_{i['change']}", uc_id=i["uc_id"],
                      summary=f"Published in v{meta['number']} — {what}", suite_version_id=vid,
                      detail={"fields": i["fields"], "moved": i["moved"] or None,
                              "retest": i["retest"] or None, "label": label})
        conn.commit()
    return {"created": True, "version": meta, "summary": changes["summary"]}


def snapshot(owner_id: int, project: str, by: int | None, source: str = "publish",
             label: str | None = None) -> dict:
    """Freeze the live use cases of `project` into a new version (or return the latest one
    unchanged when nothing differs). Returns {id, number, created}. No review / no audit —
    for baselines and internal callers; the app's publish goes through `publish`."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select max(number) from use_case_suite_version where owner_id = %s and project = %s",
                    (owner_id, project))
        before = cur.fetchone()[0]
        cur.execute("select use_case_snapshot(%s, %s, %s, %s, %s)", (owner_id, project, by, source, label))
        vid = cur.fetchone()[0]
        cur.execute("select number from use_case_suite_version where id = %s", (vid,))
        number = cur.fetchone()[0]
        conn.commit()
    return {"id": vid, "number": number, "created": number != before}


def list_versions(owner_id: int, project: str) -> list[dict]:
    """Newest first, with who built each one and how many runs used it."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select id from use_case_suite_version where owner_id = %s and project = %s "
                    "order by number desc", (owner_id, project))
        return [_version_meta(cur, r[0]) for r in cur.fetchall()]


def set_label(owner_id: int, project: str, version_id: int, label: str | None) -> dict:
    """A version's label is the one thing that stays editable (plus archiving)."""
    with pool().connection() as conn, conn.cursor() as cur:
        _own_version(cur, owner_id, project, version_id)
        cur.execute("update use_case_suite_version set label = %s where id = %s",
                    ((label or "").strip()[:200] or None, version_id))
        meta = _version_meta(cur, version_id)
        conn.commit()
    return meta


def purge_project(cur, owner_id: int, project: str) -> None:
    """BRD deletion — in the caller's tx, AFTER results and cycles are gone (they RESTRICT the
    versions/content they reference). Members cascade with their version."""
    cur.execute("delete from use_case_suite_version where owner_id = %s and project = %s", (owner_id, project))
    cur.execute("delete from use_case_content where owner_id = %s and project = %s", (owner_id, project))
    cur.execute("delete from use_case_req_text where owner_id = %s and project = %s", (owner_id, project))

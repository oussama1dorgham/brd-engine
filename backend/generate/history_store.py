"""Persist conversations and turns to Postgres (the conversation/message tables).

Kept separate from ChatSession so it's independently testable and so a DB failure
can be caught without breaking the chat.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from psycopg.types.json import Json

from ..db import pool


def create_conversation(user_id: str | None = None, project_scope: str | None = None) -> int:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into conversation (user_id, project_scope) values (%s, %s) returning id",
            (user_id, project_scope),
        )
        conversation_id = cur.fetchone()[0]
        conn.commit()
    return conversation_id


def set_conversation_project(conversation_id: int, project: str | None) -> None:
    """Lock a conversation to a BRD (used on its first message).

    Only sets project_scope when it is still NULL, so the choice is committed once
    and later messages can't silently re-scope the conversation.
    """
    if not project:
        return
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update conversation set project_scope = %s where id = %s and project_scope is null",
            (project, conversation_id),
        )
        conn.commit()


def save_message(conversation_id: int, role: str, text: str,
                 cited_chunk_ids: list[int] | None = None, usage: dict | None = None) -> int:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """insert into message (conversation_id, role, text, cited_chunk_ids, usage_json)
               values (%s, %s, %s, %s, %s) returning id""",
            (conversation_id, role, text, cited_chunk_ids, Json(usage) if usage else None),
        )
        message_id = cur.fetchone()[0]
        conn.commit()
    return message_id


def list_conversations(user_id: str, limit: int = 30, before_id: int | None = None) -> list[dict]:
    """A page of a user's conversations, newest first, each with its first question
    as a preview. Keyset pagination: pass before_id (the last id you've seen) to get
    the next older page. id is monotonic, so this is stable under concurrent inserts
    (no skips/dupes as offset pagination would have)."""
    where = "where c.user_id = %s"
    params: list = [user_id]
    if before_id is not None:
        where += " and c.id < %s"
        params.append(before_id)
    params.append(limit)
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""select c.id, c.created_at, c.project_scope,
                      (select m.text from message m
                       where m.conversation_id = c.id and m.role = 'user'
                       order by m.id limit 1) as preview
               from conversation c
               {where}
               order by c.id desc
               limit %s""",
            params,
        )
        return [
            {"id": cid, "created_at": created.isoformat(), "project": proj, "preview": preview}
            for cid, created, proj, preview in cur.fetchall()
        ]


def delete_conversation(conversation_id: int, user_id: str) -> int:
    """Delete a conversation (and its messages, via cascade) if it belongs to user."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "delete from conversation where id = %s and user_id = %s",
            (conversation_id, user_id),
        )
        deleted = cur.rowcount
        conn.commit()
    return deleted


def get_messages(conversation_id: int) -> list[dict]:
    """A conversation's turns as [{role, content, sources?}].

    Assistant turns get their cited sources reconstructed from cited_chunk_ids
    (joined to chunk/document for labels), so a RELOADED conversation shows the
    same source chips + citation tooltips as the live answer did. The stored
    cited_chunk_ids are in ascending-citation-number order, so they zip with the
    sorted [n] markers found in the text.
    """
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "select role, text, cited_chunk_ids from message where conversation_id = %s order by id",
            (conversation_id,),
        )
        rows = cur.fetchall()
        all_ids: set[int] = set()
        for _role, _text, cids in rows:
            if cids:
                all_ids.update(cids)
        labels: dict[int, dict] = {}
        if all_ids:
            cur.execute(
                """select c.id, c.req_id, c.section, d.project, c.text
                   from brd_chunk c join brd_document d on d.id = c.document_id
                   where c.id = ANY(%s)""",
                (list(all_ids),),
            )
            for cid, req_id, section, project, text in cur.fetchall():
                clean = " ".join((text or "").split())
                snip = (clean[:44].rstrip() + "…") if len(clean) > 44 else (clean or None)
                full = (clean[:600].rstrip() + "…") if len(clean) > 600 else (clean or None)
                labels[cid] = {"req_id": req_id, "section": section, "project": project,
                               "snippet": snip, "full": full}

    out: list[dict] = []
    for role, text, cids in rows:
        msg = {"role": role, "content": text}
        if role == "assistant" and cids:
            ns = sorted({int(m) for m in re.findall(r"\[(\d+)\]", text)})
            sources = [{"n": n, **labels[cid]} for n, cid in zip(ns, cids) if cid in labels]
            if sources:
                msg["sources"] = sources
        out.append(msg)
    return out


def list_projects(owner_id: int) -> list[dict]:
    """This user's BRDs (projects), with display title, doc count, and status."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """select project, min(title), count(*),
                      count(*) filter (where status = 'processing'),
                      count(*) filter (where status = 'failed'),
                      coalesce(sum(chunks_done), 0), coalesce(sum(chunks_total), 0),
                      min(proc_started_at)
               from brd_document where owner_id = %s group by project order by project""",
            (owner_id,),
        )
        rows = cur.fetchall()
    now = datetime.now(timezone.utc)
    out = []
    for p, t, n, n_proc, n_fail, done, total, started in rows:
        status = "processing" if n_proc else "failed" if n_fail else "ready"
        row = {"project": p, "title": t, "docs": n, "status": status}
        if status == "processing":
            row["progress"] = round(100 * done / total) if total else 0
            # ETA from observed pace (accounts for the rate-limit throttle)
            if done > 0 and total > done and started is not None:
                elapsed = (now - started).total_seconds()
                if elapsed > 0:
                    row["eta_seconds"] = round(elapsed / done * (total - done))
        out.append(row)
    return out


def rename_project(project: str, title: str, owner_id: int) -> int:
    """Rename a BRD's display title (every document under the slug).

    Only touches `brd_document.title` — the `project` slug (the isolation key used
    by conversations and retrieval) is left unchanged, so nothing else is affected.
    Returns the number of documents updated.
    """
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update brd_document set title = %s where project = %s and owner_id = %s",
            (title, project, owner_id),
        )
        updated = cur.rowcount
        conn.commit()
    return updated


def delete_project(project: str, owner_id: int) -> int:
    """Delete a BRD and all its chunks/embeddings (ON DELETE CASCADE).

    Conversations scoped to it are left intact (they simply retrieve nothing
    until another BRD is selected). Returns the number of documents removed.
    """
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("delete from brd_document where project = %s and owner_id = %s", (project, owner_id))
        n = cur.rowcount
        conn.commit()
    # Drop this owner's stale retrieve/answer cache for the deleted project.
    if n:
        from .. import cache
        cache.bust_project(project, owner_id=owner_id)
    return n


def conversation_user_id(conversation_id: int) -> str | None:
    """The owning user_id of a conversation, or None if it doesn't exist."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select user_id from conversation where id = %s", (conversation_id,))
        row = cur.fetchone()
        return row[0] if row else None


def get_conversation(conversation_id: int) -> dict | None:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select id, project_scope, model from conversation where id = %s", (conversation_id,))
        row = cur.fetchone()
        return {"id": row[0], "project_scope": row[1], "model": row[2]} if row else None


def set_conversation_model(conversation_id: int, model: str | None) -> None:
    """Remember the LLM model picked for a conversation (BYOK model picker)."""
    if not model:
        return
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update conversation set model = %s where id = %s", (model, conversation_id))
        conn.commit()

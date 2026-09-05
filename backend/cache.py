"""Fail-open, Postgres-backed cache for the RAG pipeline's remote calls.

Three namespaces memoize the paid / rate-limited stages of a question:
  embed     (text, model)                -> query vector          [immutable]
  retrieve  (query, owner, project, ...) -> ranked chunk results  [bust on ingest]
  answer    (standalone, chunk_ids, gen) -> full response object  [bust on ingest]

INVARIANTS (see the caching diagnosis in CLAUDE.md history):

  1. FAIL-OPEN. Any error — DB down, bad row, serialization issue — is swallowed
     and treated as a cache miss. The cache can never raise into a request or
     change correctness; the worst it can do is fail to save a call.

  2. NEVER NEST POOL CHECKOUTS. get()/set()/bust each take ONE short-lived
     connection and release it before returning. The pool is small (max_size=10),
     so a caller must call these OUTSIDE (never inside) its own
     `with pool().connection()` block, or concurrency can exhaust the pool.

  3. MODEL ID IN EVERY KEY. embed keys carry the embed model, answer keys carry
     GEN_MODEL — so swapping a model self-invalidates instead of serving stale
     output from the previous model.

Correctness of the corpus-dependent namespaces (retrieve/answer) comes from
bust_project() being called on every ingest/delete; TTL is only a safety net.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from .config import settings
from .db import pool

log = logging.getLogger("brd.cache")

# Namespaces
EMBED = "embed"
RETRIEVE = "retrieve"
ANSWER = "answer"


def enabled() -> bool:
    return settings.cache_enabled


def _key(namespace: str, parts: Any) -> str:
    """Stable content hash of the namespace + canonical inputs.

    json.dumps(sort_keys=True) makes dict ordering irrelevant; the namespace
    prefix keeps identical inputs in different layers from colliding.
    """
    canonical = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{namespace}\x00{canonical}".encode("utf-8")).hexdigest()


def get(namespace: str, parts: Any, *, ttl_seconds: int | None = None) -> Any | None:
    """Return the cached value for (namespace, parts), or None on miss/disabled/error.

    ttl_seconds: rows older than this are treated as a miss. None uses the
    configured default (0 = no age limit). 'embed' should pass 0 — it never expires.
    """
    if not settings.cache_enabled:
        return None
    ttl = settings.cache_ttl_seconds if ttl_seconds is None else ttl_seconds
    key = _key(namespace, parts)
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            if ttl and ttl > 0:
                cur.execute(
                    "select value from cache_kv "
                    "where key = %s and created_at > now() - make_interval(secs => %s)",
                    (key, ttl),
                )
            else:
                cur.execute("select value from cache_kv where key = %s", (key,))
            row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:  # noqa: BLE001 — fail-open: any error is a miss
        log.debug("cache get miss (%s): %s: %s", namespace, type(e).__name__, e)
        return None


def set(namespace: str, parts: Any, value: Any, *,
        owner_id: int | None = None, project: str | None = None) -> None:
    """Store value under (namespace, parts). Best-effort; never raises.

    owner_id/project are stored (not part of the key hash) so bust_project() can
    delete a project's entries with an indexed WHERE.
    """
    if not settings.cache_enabled:
        return
    key = _key(namespace, parts)
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """insert into cache_kv (key, namespace, owner_id, project, value)
                   values (%s, %s, %s, %s, %s)
                   on conflict (key) do update set
                     value = excluded.value, created_at = now()""",
                (key, namespace, owner_id, project, json.dumps(value, ensure_ascii=False)),
            )
    except Exception as e:  # noqa: BLE001 — fail-open: a failed save just costs a call later
        log.debug("cache set skipped (%s): %s: %s", namespace, type(e).__name__, e)


_ANY_OWNER = object()  # sentinel: bust a project across every owner


def bust_project(project: str | None, *, owner_id: Any = _ANY_OWNER,
                 namespaces: tuple[str, ...] = (RETRIEVE, ANSWER)) -> int:
    """Delete corpus-dependent cache entries for one project after an ingest/delete.

    Coarse on purpose: any change to a project's chunks can shift retrieval, so we
    drop the whole project's retrieve/answer entries. 'embed' is never busted
    (immutable). By default busts across ALL owners of that project name (the
    ingest path doesn't carry an owner id); pass owner_id to narrow it.
    Returns rows deleted (0 on error/disabled). Never raises.
    """
    if not settings.cache_enabled:
        return 0
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            if owner_id is _ANY_OWNER:
                cur.execute(
                    "delete from cache_kv where namespace = any(%s) "
                    "and project is not distinct from %s",
                    (list(namespaces), project),
                )
            else:
                cur.execute(
                    "delete from cache_kv where namespace = any(%s) "
                    "and owner_id is not distinct from %s and project is not distinct from %s",
                    (list(namespaces), owner_id, project),
                )
            return max(cur.rowcount, 0)
    except Exception as e:  # noqa: BLE001
        log.debug("cache bust skipped (%s): %s: %s", project, type(e).__name__, e)
        return 0


def prune_expired(ttl_seconds: int | None = None) -> int:
    """Delete corpus-dependent entries older than the TTL. For a periodic sweep.

    Never touches 'embed' (immutable). Returns rows deleted. Never raises.
    """
    ttl = settings.cache_ttl_seconds if ttl_seconds is None else ttl_seconds
    if not settings.cache_enabled or not ttl or ttl <= 0:
        return 0
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "delete from cache_kv where namespace <> %s "
                "and created_at < now() - make_interval(secs => %s)",
                (EMBED, ttl),
            )
            return max(cur.rowcount, 0)
    except Exception as e:  # noqa: BLE001
        log.debug("cache prune skipped: %s: %s", type(e).__name__, e)
        return 0

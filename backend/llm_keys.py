"""Per-user provider API keys for bring-your-own-key (BYOK) LLM.

A user may store MANY keys (across providers); exactly one is ACTIVE at a time
and drives generation. Each key keeps its own preferred-model subset. Keys are
encrypted at rest (backend/crypto.py); model listing + generation route through
the provider adapters (backend/providers) via backend/generate/engine.
"""
from __future__ import annotations

import logging

from psycopg.types.json import Jsonb

from . import cache, crypto
from .db import pool
from .generate import engine
from .providers.base import ProviderError

log = logging.getLogger("brd.llm_keys")

_MODELS_NS = "llm_models"
_MODELS_TTL = 900  # 15 min — model lists change rarely


class InvalidKey(Exception):
    """The provided provider/base_url/API key could not list models."""


def _mask(key: str) -> str:
    key = key.strip()
    if len(key) <= 8:
        return "…" + key[-2:]
    return f"{key[:3]}…{key[-4:]}"


def validate_and_list(provider: str, base_url: str | None, api_key: str) -> list[str]:
    """Confirm the key works by listing its models (via the provider adapter)."""
    try:
        models = engine.list_models(provider, api_key, base_url or None)
    except ProviderError as e:
        raise InvalidKey(str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise InvalidKey(str(e)) from e
    if not models:
        raise InvalidKey("the key authenticated but returned no models")
    return models


# --- listing / adding / activating / deleting ------------------------------

def list_keys(user_id: int) -> list[dict]:
    """All of a user's keys (no plaintext), active first."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "select id, provider, base_url, api_key_masked, is_active, label "
            "from user_llm_key where user_id = %s order by is_active desc, id",
            (user_id,),
        )
        rows = cur.fetchall()
    return [{"id": r[0], "provider": r[1], "base_url": r[2], "masked": r[3],
             "is_active": r[4], "label": r[5]} for r in rows]


def add_key(user_id: int, provider: str, base_url: str, api_key: str,
            label: str | None = None, models: list[str] | None = None) -> dict:
    """Store a new key (encrypted) and make it the active one. Returns its metadata."""
    enc = crypto.encrypt(api_key)
    masked = _mask(api_key)
    base_url = (base_url or "").strip()
    label = (label or "").strip() or None
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update user_llm_key set is_active = false where user_id = %s", (user_id,))
        cur.execute(
            "insert into user_llm_key "
            "(user_id, provider, base_url, api_key_enc, api_key_masked, preferred_models, is_active, label) "
            "values (%s, %s, %s, %s, %s, %s, true, %s) returning id",
            (user_id, provider, base_url, enc, masked, Jsonb([]), label),
        )
        key_id = cur.fetchone()[0]
        conn.commit()
    if models is not None:
        cache.set(_MODELS_NS, [key_id], models, owner_id=user_id, project=str(user_id))
    return {"id": key_id, "provider": provider, "base_url": base_url, "masked": masked,
            "is_active": True, "label": label}


def set_active(user_id: int, key_id: int) -> bool:
    """Make key_id the active one for the user. False if it isn't theirs."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select 1 from user_llm_key where id = %s and user_id = %s", (key_id, user_id))
        if not cur.fetchone():
            return False
        # Two steps so we never transiently have two active rows (the partial unique
        # index "one active per user" is non-deferrable and would reject that).
        cur.execute("update user_llm_key set is_active = false where user_id = %s and is_active", (user_id,))
        cur.execute("update user_llm_key set is_active = true, updated_at = now() "
                    "where id = %s and user_id = %s", (key_id, user_id))
        conn.commit()
    return True


def delete_key(user_id: int, key_id: int) -> bool:
    """Delete one key; if it was active, activate the newest remaining one."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select is_active from user_llm_key where id = %s and user_id = %s", (key_id, user_id))
        row = cur.fetchone()
        if not row:
            return False
        was_active = row[0]
        cur.execute("delete from user_llm_key where id = %s and user_id = %s", (key_id, user_id))
        if was_active:
            cur.execute(
                "update user_llm_key set is_active = true where id = "
                "(select id from user_llm_key where user_id = %s order by id desc limit 1)",
                (user_id,),
            )
        conn.commit()
    cache.bust_project(str(user_id), namespaces=(_MODELS_NS,))
    return True


# --- the active key (drives generation) ------------------------------------

def _active_row(user_id: int):
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select id, provider, base_url, api_key_enc from user_llm_key "
                    "where user_id = %s and is_active limit 1", (user_id,))
        return cur.fetchone()


def get_meta(user_id: int) -> dict | None:
    """Active key display metadata (never plaintext), or None if the user has none."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select provider, base_url, api_key_masked, label from user_llm_key "
                    "where user_id = %s and is_active limit 1", (user_id,))
        row = cur.fetchone()
    if not row:
        return None
    return {"configured": True, "provider": row[0], "base_url": row[1], "masked": row[2], "label": row[3]}


def get_secret(user_id: int) -> tuple[str, str | None, str] | None:
    """(provider, base_url, api_key) for the ACTIVE key — generation only. None if unset/broken."""
    row = _active_row(user_id)
    if not row:
        return None
    try:
        return row[1], (row[2] or None), crypto.decrypt(row[3])
    except Exception:  # noqa: BLE001 — a broken/rotated secret must not crash generation
        log.warning("could not decrypt active LLM key for user %s", user_id)
        return None


# --- per-key model listing + preferred subset ------------------------------

def _key_row(user_id: int, key_id: int):
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select provider, base_url, api_key_enc from user_llm_key "
                    "where id = %s and user_id = %s", (key_id, user_id))
        return cur.fetchone()


def list_models_for(user_id: int, key_id: int, force: bool = False) -> list[str]:
    """Models a specific key can use (cached per key). [] if missing / list fails."""
    row = _key_row(user_id, key_id)
    if not row:
        return []
    provider, base_url, enc = row
    if not force:
        hit = cache.get(_MODELS_NS, [key_id], ttl_seconds=_MODELS_TTL)
        if hit is not None:
            return hit
    try:
        models = validate_and_list(provider, base_url, crypto.decrypt(enc))
    except (InvalidKey, Exception):  # noqa: BLE001
        return []
    cache.set(_MODELS_NS, [key_id], models, owner_id=user_id, project=str(user_id))
    return models


def active_key_id(user_id: int) -> int | None:
    row = _active_row(user_id)
    return row[0] if row else None


def list_models(user_id: int, force: bool = False) -> list[str]:
    """Models the ACTIVE key can use (for the in-chat picker)."""
    kid = active_key_id(user_id)
    return list_models_for(user_id, kid, force) if kid else []


def get_preferred(user_id: int, key_id: int | None = None) -> list[str]:
    """Preferred model subset for a key (defaults to the active key)."""
    kid = key_id or active_key_id(user_id)
    if not kid:
        return []
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select preferred_models from user_llm_key where id = %s and user_id = %s",
                    (kid, user_id))
        row = cur.fetchone()
    if not row or not row[0]:
        return []
    val = row[0]
    return [m for m in val if isinstance(m, str)] if isinstance(val, list) else []


def set_preferred(user_id: int, models: list[str], key_id: int | None = None) -> list[str]:
    """Store a key's preferred subset (deduped, filtered to ids it can use)."""
    kid = key_id or active_key_id(user_id)
    if not kid:
        return []
    seen: set[str] = set()
    clean: list[str] = []
    for m in models:
        m = (m or "").strip()
        if m and m not in seen:
            seen.add(m)
            clean.append(m)
    available = list_models_for(user_id, kid)
    if available:
        allowed = set(available)
        clean = [m for m in clean if m in allowed]
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update user_llm_key set preferred_models = %s, updated_at = now() "
                    "where id = %s and user_id = %s", (Jsonb(clean), kid, user_id))
        conn.commit()
    return clean

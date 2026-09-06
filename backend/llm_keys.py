"""Per-user provider API keys for the bring-your-own-key (BYOK) LLM feature.

OpenAI-compatible providers only: a user stores a base_url + API key, we validate
it by listing models, and generation uses their key + chosen model. Keys are
encrypted at rest (backend/crypto.py); only a masked hint is ever returned.
"""
from __future__ import annotations

import logging

from openai import OpenAI
from psycopg.types.json import Jsonb

from . import cache, crypto
from .db import pool

log = logging.getLogger("brd.llm_keys")

_MODELS_NS = "llm_models"
_MODELS_TTL = 900  # 15 min — model lists change rarely


class InvalidKey(Exception):
    """The provided base_url/API key could not list models."""


def _mask(key: str) -> str:
    key = key.strip()
    if len(key) <= 8:
        return "…" + key[-2:]
    return f"{key[:3]}…{key[-4:]}"


def validate_and_list(base_url: str, api_key: str) -> list[str]:
    """Confirm the key works by listing models. Raises InvalidKey on failure."""
    try:
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=15)
        resp = client.models.list()
    except Exception as e:  # noqa: BLE001 — surface a clean error to the caller
        raise InvalidKey(str(e)) from e
    models = sorted({m.id for m in getattr(resp, "data", []) if getattr(m, "id", None)})
    if not models:
        raise InvalidKey("the key authenticated but returned no models")
    return models


def set_key(user_id: int, base_url: str, api_key: str, models: list[str] | None = None) -> dict:
    """Encrypt + store the user's key (upsert). Returns display metadata."""
    enc = crypto.encrypt(api_key)
    masked = _mask(api_key)
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """insert into user_llm_key (user_id, base_url, api_key_enc, api_key_masked)
               values (%s, %s, %s, %s)
               on conflict (user_id) do update set
                 base_url = excluded.base_url, api_key_enc = excluded.api_key_enc,
                 api_key_masked = excluded.api_key_masked, updated_at = now()""",
            (user_id, base_url.strip(), enc, masked),
        )
        conn.commit()
    # refresh the cached model list for this user
    cache.bust_project(str(user_id), namespaces=(_MODELS_NS,))
    if models is not None:
        cache.set(_MODELS_NS, [user_id, base_url.strip()], models,
                  owner_id=user_id, project=str(user_id))
    return {"configured": True, "masked": masked, "base_url": base_url.strip()}


def get_meta(user_id: int) -> dict | None:
    """Display metadata (never the plaintext key), or None if unset."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select base_url, api_key_masked from user_llm_key where user_id = %s", (user_id,))
        row = cur.fetchone()
    if not row:
        return None
    return {"configured": True, "base_url": row[0], "masked": row[1]}


def get_secret(user_id: int) -> tuple[str, str] | None:
    """(base_url, api_key) decrypted — for generation only. None if unset/broken."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select base_url, api_key_enc from user_llm_key where user_id = %s", (user_id,))
        row = cur.fetchone()
    if not row:
        return None
    try:
        return row[0], crypto.decrypt(row[1])
    except Exception:  # noqa: BLE001 — a broken/rotated secret must not crash generation
        log.warning("could not decrypt stored LLM key for user %s", user_id)
        return None


def delete_key(user_id: int) -> None:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("delete from user_llm_key where user_id = %s", (user_id,))
        conn.commit()
    cache.bust_project(str(user_id), namespaces=(_MODELS_NS,))


def get_preferred(user_id: int) -> list[str]:
    """The user's curated subset of models to show in the in-chat picker.

    [] means "not curated yet" — the UI then falls back to showing every model
    the key can reach, so nothing breaks before the user picks favourites.
    """
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select preferred_models from user_llm_key where user_id = %s", (user_id,))
        row = cur.fetchone()
    if not row or not row[0]:
        return []
    val = row[0]  # psycopg parses jsonb to a Python list
    return [m for m in val if isinstance(m, str)] if isinstance(val, list) else []


def set_preferred(user_id: int, models: list[str]) -> list[str]:
    """Store the user's preferred model subset (deduped, order-preserved).

    When we can list the key's models, keep only ids it can actually use — so a
    stale or hand-edited selection never pins a model the provider won't serve.
    Returns the stored list.
    """
    seen: set[str] = set()
    clean: list[str] = []
    for m in models:
        m = (m or "").strip()
        if m and m not in seen:
            seen.add(m)
            clean.append(m)
    available = list_models(user_id)
    if available:
        allowed = set(available)
        clean = [m for m in clean if m in allowed]
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update user_llm_key set preferred_models = %s, updated_at = now() where user_id = %s",
            (Jsonb(clean), user_id),
        )
        conn.commit()
    return clean


def list_models(user_id: int, force: bool = False) -> list[str]:
    """Models the user's key can use (cached). [] if no key / list fails."""
    secret = get_secret(user_id)
    if not secret:
        return []
    base_url, api_key = secret
    if not force:
        hit = cache.get(_MODELS_NS, [user_id, base_url], ttl_seconds=_MODELS_TTL)
        if hit is not None:
            return hit
    try:
        models = validate_and_list(base_url, api_key)
    except InvalidKey:
        return []
    cache.set(_MODELS_NS, [user_id, base_url], models, owner_id=user_id, project=str(user_id))
    return models

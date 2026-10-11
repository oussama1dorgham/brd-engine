"""Live provider usage for the UI — "how much is left".

* OpenRouter reports it: GET {base}/key returns the key's free-model requests used / limit /
  remaining today and its credit usage. The call is free and doesn't count against the limit.
  Cached per key for a minute (a forced refresh at most every 10 s) so polling costs nothing.
  Works for the server key and for a user's own OpenRouter key; other providers don't expose it.
* Voyage reports nothing remaining (only a free-tier warning header), so embedding capacity is
  read from our own pacing ledger (core.voyage.budget) — per server process.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from datetime import datetime, timezone

import httpx

from backend.core import voyage
from backend.core.config import settings

log = logging.getLogger("brd.usage")

_TTL = 60.0              # seconds a status stays fresh
_MIN_REFRESH = 10.0      # a forced refresh is honoured at most this often per key
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def is_openrouter(base_url: str | None) -> bool:
    return "openrouter.ai" in (base_url or "").lower()


def parse_key_status(data: dict) -> dict:
    """OpenRouter /key `data` → the fields the UI shows (pure; unit-tested)."""
    free = data.get("free_model_daily_requests") or None
    return {
        "available": True,
        "provider": "openrouter",
        "is_free_tier": bool(data.get("is_free_tier")),
        "free_daily": ({"used": free.get("used"), "limit": free.get("limit"), "remaining": free.get("remaining")}
                       if isinstance(free, dict) else None),
        "credits": {"used_total": data.get("usage"), "used_today": data.get("usage_daily"),
                    "limit": data.get("limit"), "remaining": data.get("limit_remaining")},
    }


def openrouter_status(api_key: str | None, base_url: str | None = None, *, force: bool = False) -> dict | None:
    """Live status of an OpenRouter key; None when the key isn't an OpenRouter key."""
    if not api_key:
        return None
    base = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
    if not is_openrouter(base):
        return None
    key = hashlib.sha256(f"{base}|{api_key}".encode()).hexdigest()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
    if hit and (now - hit[0] < (_MIN_REFRESH if force else _TTL)):
        return hit[1]
    try:
        r = httpx.get(base + "/key", headers={"Authorization": f"Bearer {api_key}"}, timeout=8)
        r.raise_for_status()
        out = parse_key_status(r.json().get("data") or {})
    except Exception as e:  # noqa: BLE001 — usage is informational; never break the page over it
        log.warning("usage: OpenRouter key status unavailable: %s", e)
        out = {"available": False, "provider": "openrouter", "error": "OpenRouter didn't answer — try again shortly."}
    out["checked_at"] = datetime.now(timezone.utc).isoformat()
    with _lock:
        _cache[key] = (now, out)
    return out


def embeddings_status() -> dict:
    """Our Voyage pacing ledger for the current minute (Voyage itself reports no remaining quota)."""
    return {"provider": "voyage", "scope": "this server process", **voyage.budget.snapshot()}

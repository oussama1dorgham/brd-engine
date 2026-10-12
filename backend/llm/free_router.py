"""OpenRouter's `openrouter/free`, made dependable.

`openrouter/free` is not a model: OpenRouter picks SOME free model for every request, and
the app makes 2–4 calls per question (condense, answer, re-ask, verify). Measured failure
modes of that pool (Oct 2026):

  * a content-SAFETY CLASSIFIER (nvidia/nemotron-3.5-content-safety) answers every prompt
    with "User Safety: safe" — which condense then used as the search query;
  * a model whose reasoning is mandatory 400s on our `reasoning: {enabled: false}`;
  * "agentic harness only" models 403; others 429 (upstream) or 504 (idle timeout).

So when the user picks `openrouter/free` we pick instead: chat-capable free models from the
live catalogue (classifiers excluded, models that just failed cooled down), tried one at a
time in a stable preference order, each reply checked before it is used. Every other model
id goes straight through unchanged.
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import urlparse

import httpx

FREE_ROUTER = "openrouter/free"
MAX_ATTEMPTS = 3
# the catalogue always comes from OpenRouter itself — never from a user-supplied base_url,
# because it is shared by every user of this process
CATALOG_URL = "https://openrouter.ai/api/v1/models"

# not chat models: classifiers / guards / embedders / speech / image models
_NOT_CHAT = re.compile(r"safety|guard|moderat|shield|classif|embed|rerank|whisper|tts|transcri|image|ocr", re.I)
# a stable, known-good order; anything else in the catalogue follows, longest context first
PREFERRED = (
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "poolside/laguna-s-2.1:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-26b-a4b-it:free",
    "apodex/apodex-1.1-mini:free",
    "poolside/laguna-xs-2.1:free",
)
_CATALOG_TTL = 3600

_lock = threading.Lock()
_catalog: tuple[float, list[str]] | None = None
_cooldown: dict[str, float] = {}          # model -> monotonic time it may be used again

# a moderation verdict instead of an answer. The WHOLE reply must be verdict lines — "User Safety:
# safe", "Response Safety: unsafe", a bare "safe"/"unsafe", "S1, S10" category codes,
# "Safety Categories: …" — so a real answer that starts "Safety: safe operating limits …" is not one.
_VERDICT_LINE = re.compile(
    r"^\W*(?:(?:(?:user|response|agent|assistant|prompt)\s+)?safety\s*:\s*(?:safe|unsafe)"
    r"|S\d+(?:\s*,\s*S\d+)*|(?:safety\s+)?categor(?:y|ies)\s*:.*)\W*$"
    r"|^(?-i:safe|unsafe)$", re.I)      # a bare classifier label is lowercase, unpunctuated


def is_router(model: str | None, base_url: str | None) -> bool:
    """`openrouter/free` on OpenRouter itself (exact host — not a look-alike domain)."""
    return model == FREE_ROUTER and is_openrouter(base_url)


def is_openrouter(base_url: str | None) -> bool:
    try:
        host = (urlparse(base_url or "").hostname or "").lower()
    except ValueError:
        return False
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def is_verdict(text: str | None) -> bool:
    """A safety classifier's verdict — never an answer, never a search query."""
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return bool(lines) and len(lines) <= 4 and all(_VERDICT_LINE.match(ln.strip()) for ln in lines)


def _fetch_catalog(base_url: str) -> list[str]:
    data = httpx.get(CATALOG_URL, timeout=10).json().get("data", [])
    out: list[tuple[int, str]] = []
    for m in data:
        mid = m.get("id") or ""
        if not mid.endswith(":free") or _NOT_CHAT.search(mid):
            continue
        outputs = ((m.get("architecture") or {}).get("output_modalities")) or ["text"]
        if "text" not in outputs:
            continue
        out.append((int(m.get("context_length") or 0), mid))
    return [mid for _, mid in sorted(out, key=lambda x: -x[0])]


def candidates(base_url: str) -> list[str]:
    """Free chat models to try, best first, without the ones cooling down. The catalogue is
    cached for an hour; if it can't be fetched the static preference list is used."""
    global _catalog
    now = time.monotonic()
    with _lock:
        cached = _catalog
    if cached is None or now - cached[0] > _CATALOG_TTL:
        try:
            ids = _fetch_catalog(base_url)
        except Exception:  # noqa: BLE001 — never fail a question because the catalogue is down
            ids = cached[1] if cached else list(PREFERRED)
        with _lock:
            _catalog = (now, ids)
        cached = (now, ids)
    ids = cached[1] or list(PREFERRED)
    ordered = [m for m in PREFERRED if m in ids] + [m for m in ids if m not in PREFERRED]
    with _lock:
        usable = [m for m in ordered if _cooldown.get(m, 0) <= now]
    return usable or ordered          # all cooling down: try anyway rather than refuse


def cool(model: str, seconds: float) -> None:
    with _lock:
        _cooldown[model] = max(_cooldown.get(model, 0), time.monotonic() + seconds)


def account_error(status: int | None, raw: str) -> bool:
    """A failure of the KEY / ACCOUNT, not of the model: a bad or revoked key (401), no credits
    (402), the account's free-model quota per day / per minute, or a 403 that isn't the
    model's "agentic harness only" gate. Another free model won't do better — report it as is,
    and never cool models down for it (the cooldown is shared by every user)."""
    raw = raw or ""
    return (status in (401, 402) or "free-models-per" in raw
            or (status == 403 and "agentic harness" not in raw))


def account_limited(raw: str) -> bool:
    """OpenRouter's per-ACCOUNT daily free quota."""
    return "free-models-per-day" in (raw or "")


def cooldown_for(status: int | None, raw: str) -> float:
    """How long a model that just failed stays out of the free pool."""
    raw = raw or ""
    if "agentic harness" in raw or status == 404:
        return 24 * 3600                       # this model can't serve this app at all
    if "Reasoning is mandatory" in raw:
        return 24 * 3600                       # rejects reasoning-off; the pool has better options
    if status == 429 or (status or 0) >= 500 or status is None:
        return 120                             # transient: rate limit / upstream timeout / network
    return 3600                                # any other rejection


def _reset_for_tests() -> None:
    global _catalog
    with _lock:
        _catalog = None
        _cooldown.clear()

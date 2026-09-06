"""LLM client for generation, routed through OpenRouter (OpenAI-compatible).

OpenRouter exposes an OpenAI-style Chat Completions API, so we use the openai
SDK pointed at the OpenRouter base URL. GEN_MODEL in .env selects the model.

Free models share a rate-limited upstream pool, so chat() retries on 429 with
backoff (honoring Retry-After when present).

    python -m backend.generate.llm          # connectivity ping
"""
from __future__ import annotations

import hashlib
import time

from openai import OpenAI, RateLimitError

from ..config import settings

_client: OpenAI | None = None
_byok_clients: dict[str, OpenAI] = {}   # per (base_url, api_key) — for bring-your-own-key

# Optional attribution headers OpenRouter uses for its dashboard/leaderboards.
_HEADERS = {"HTTP-Referer": "http://localhost", "X-Title": "BRD Retrieval Engine"}


def client(api_key: str | None = None, base_url: str | None = None) -> OpenAI:
    """Return an OpenAI-compatible client.

    With no args, the shared system client (OPENROUTER_* from .env). With a
    per-user api_key + base_url (bring-your-own-key), a client for that key,
    memoized so we don't rebuild it per request.
    """
    if not api_key or not base_url:
        global _client
        if _client is None:
            # max_retries=0: chat() owns the retry policy, so the SDK doesn't add
            # its own hidden 429 retries on top (which multiplied the wait).
            _client = OpenAI(
                api_key=settings.require_openrouter(),
                base_url=settings.require_base_url(),
                timeout=60,
                max_retries=0,
            )
        return _client
    cache_key = hashlib.sha256(f"{base_url}\x00{api_key}".encode()).hexdigest()
    c = _byok_clients.get(cache_key)
    if c is None:
        c = OpenAI(api_key=api_key, base_url=base_url, timeout=60, max_retries=0)
        _byok_clients[cache_key] = c
    return c


def _is_hard_limit(err: RateLimitError) -> bool:
    """A quota that won't recover by retrying now (e.g. OpenRouter's free per-day
    cap), as opposed to a transient per-minute burst. Retrying a hard limit just
    makes the user wait minutes for the same 429 — so we fail fast instead."""
    try:
        text = str(getattr(err, "message", "") or err).lower()
    except Exception:
        text = ""
    return any(s in text for s in ("per-day", "per day", "daily", "free-models-per-day", "insufficient", "quota"))


def chat(messages: list[dict], model: str | None = None, temperature: float = 0.0,
         max_tokens: int = 600, stream: bool = False, max_retries: int = 3,
         api_key: str | None = None, base_url: str | None = None):
    """Create a chat completion, retrying briefly on TRANSIENT 429 rate limits.

    A hard/daily quota (e.g. OpenRouter's free per-day cap) is raised immediately
    — retrying only makes the user wait minutes for the same error. Transient
    per-minute bursts get a small, bounded backoff (~a few seconds), never the
    multi-minute spin that left the UI stuck.

    Pass api_key + base_url to route through a user's own provider (BYOK); model
    then selects one of that key's models. Otherwise uses the system GEN_MODEL."""
    kwargs = dict(
        model=settings.need(model or settings.gen_model, "GEN_MODEL"),
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=stream,
        extra_headers=_HEADERS,
    )
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            return client(api_key, base_url).chat.completions.create(**kwargs)
        except RateLimitError as err:
            if _is_hard_limit(err):
                raise               # won't clear by waiting — surface it now
            last_err = err
            retry_after = 4
            try:
                header = err.response.headers.get("retry-after")
                if header:
                    retry_after = int(header)
            except Exception:
                pass
            time.sleep(min(retry_after, 8) + attempt * 2)   # ~4,6,8s — bounded
    raise last_err  # type: ignore[misc]


def message_text(resp) -> str:
    """Safely extract assistant text from a chat completion.

    Some free providers occasionally return a 200 whose `choices` is None or
    whose `message`/`content` is missing; read it defensively so a bad response
    degrades to an empty string instead of crashing with a subscript TypeError.
    """
    choices = getattr(resp, "choices", None)
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    if message is None:
        return ""
    return getattr(message, "content", None) or ""


if __name__ == "__main__":
    resp = chat(
        [{"role": "user", "content": "Reply with exactly: BRD engine online."}],
        max_tokens=20,
    )
    print("model :", resp.model)
    print("reply :", message_text(resp))
    if resp.usage:
        print("tokens:", resp.usage.prompt_tokens, "in /", resp.usage.completion_tokens, "out")

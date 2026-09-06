"""Provider-agnostic generation routing.

The generation path (session/condense/refine) calls this instead of a specific
SDK. It resolves credentials (a user's BYOK provider, or the system OpenRouter
fallback), dispatches to the right adapter, and applies a bounded retry on
transient 429s. Adapters raise ProviderError with a user-facing message.
"""
from __future__ import annotations

import time
from typing import Iterator

from ..config import settings
from ..providers import registry
from ..providers.base import ProviderError


def _resolve(provider: str | None, api_key: str | None, base_url: str | None):
    """(provider, api_key, base_url) → concrete creds. No key ⇒ system fallback
    (OpenRouter via the OpenAI-compatible adapter)."""
    if api_key:
        return (provider or registry.DEFAULT_PROVIDER), api_key, base_url
    return registry.DEFAULT_PROVIDER, settings.require_openrouter(), settings.require_base_url()


def list_models(provider: str | None, api_key: str, base_url: str | None) -> list[str]:
    p, k, b = _resolve(provider, api_key, base_url)
    return registry.get(p).list_models(k, b)


def _model(model: str | None) -> str:
    return settings.need(model or settings.gen_model, "GEN_MODEL")


def complete_chat(messages: list[dict], *, provider: str | None = None, api_key: str | None = None,
                  base_url: str | None = None, model: str | None = None,
                  temperature: float = 0.0, max_tokens: int = 600, max_retries: int = 3) -> str:
    p, k, b = _resolve(provider, api_key, base_url)
    adapter, m = registry.get(p), _model(model)
    last: ProviderError | None = None
    for attempt in range(max_retries):
        try:
            return adapter.complete(k, b, m, messages, temperature=temperature, max_tokens=max_tokens)
        except ProviderError as e:
            if e.status != 429:        # only transient rate limits are worth retrying
                raise
            last = e
            time.sleep(min(4 + attempt * 2, 8))
    raise last  # type: ignore[misc]


def stream_chat(messages: list[dict], *, provider: str | None = None, api_key: str | None = None,
                base_url: str | None = None, model: str | None = None,
                temperature: float = 0.0, max_tokens: int = 600) -> Iterator[str]:
    p, k, b = _resolve(provider, api_key, base_url)
    yield from registry.get(p).stream(k, b, _model(model), messages,
                                      temperature=temperature, max_tokens=max_tokens)

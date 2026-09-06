"""Provider registry: stored provider id -> adapter, plus UI metadata."""
from __future__ import annotations

from .anthropic import AnthropicAdapter
from .base import Adapter
from .cohere import CohereAdapter
from .gemini import GeminiAdapter
from .openai_compat import OpenAICompatAdapter

DEFAULT_PROVIDER = "openai"

_ADAPTERS: dict[str, Adapter] = {
    a.key: a for a in (OpenAICompatAdapter(), AnthropicAdapter(), GeminiAdapter(), CohereAdapter())
}


def get(provider: str | None) -> Adapter:
    """Adapter for a provider id, falling back to the OpenAI-compatible one."""
    return _ADAPTERS.get(provider or DEFAULT_PROVIDER, _ADAPTERS[DEFAULT_PROVIDER])


def is_known(provider: str | None) -> bool:
    return (provider or DEFAULT_PROVIDER) in _ADAPTERS


def providers_meta() -> list[dict]:
    """For the settings UI: which providers exist and whether each needs a base_url."""
    return [
        {"key": a.key, "label": a.label, "needs_base_url": a.needs_base_url,
         "default_base_url": a.default_base_url}
        for a in _ADAPTERS.values()
    ]

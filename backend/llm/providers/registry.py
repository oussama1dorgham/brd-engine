"""Provider registry: stored provider id -> adapter, plus UI metadata."""
from __future__ import annotations

from backend.llm.providers.anthropic import AnthropicAdapter
from backend.llm.providers.base import Adapter
from backend.llm.providers.cohere import CohereAdapter
from backend.llm.providers.gemini import GeminiAdapter
from backend.llm.providers.openai_compat import ExperientialAdapter, OpenAICompatAdapter

DEFAULT_PROVIDER = "openai"

_ADAPTERS: dict[str, Adapter] = {
    a.key: a for a in (OpenAICompatAdapter(), ExperientialAdapter(),
                       AnthropicAdapter(), GeminiAdapter(), CohereAdapter())
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

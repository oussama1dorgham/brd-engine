"""Adapter for any OpenAI-compatible provider (OpenAI, Groq, Together, Fireworks,
DeepInfra, Mistral, xAI, Perplexity, DeepSeek, Google's OpenAI-compat endpoint,
local Ollama/LM Studio/vLLM, OpenRouter, …). The user supplies base_url + key."""
from __future__ import annotations

from typing import Iterator

from openai import OpenAI

from ..generate.llm import describe_error
from .base import ProviderError


class OpenAICompatAdapter:
    key = "openai"
    label = "OpenAI-compatible"
    needs_base_url = True
    default_base_url = "https://api.openai.com/v1"

    def _client(self, api_key: str, base_url: str | None) -> OpenAI:
        return OpenAI(api_key=api_key, base_url=base_url, timeout=60, max_retries=0)

    def _wrap(self, e: Exception) -> ProviderError:
        return ProviderError(describe_error(e), getattr(e, "status_code", None))

    def list_models(self, api_key: str, base_url: str | None) -> list[str]:
        try:
            resp = self._client(api_key, base_url).models.list()
        except Exception as e:  # noqa: BLE001
            raise self._wrap(e) from e
        return sorted({m.id for m in getattr(resp, "data", []) if getattr(m, "id", None)})

    def complete(self, api_key, base_url, model, messages, *, temperature, max_tokens) -> str:
        try:
            resp = self._client(api_key, base_url).chat.completions.create(
                model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
        except Exception as e:  # noqa: BLE001
            raise self._wrap(e) from e
        ch = getattr(resp, "choices", None)
        if not ch:
            return ""
        msg = getattr(ch[0], "message", None)
        return (getattr(msg, "content", None) or "") if msg else ""

    def stream(self, api_key, base_url, model, messages, *, temperature, max_tokens) -> Iterator[str]:
        try:
            s = self._client(api_key, base_url).chat.completions.create(
                model=model, messages=messages, temperature=temperature, max_tokens=max_tokens, stream=True)
            for chunk in s:
                if not chunk.choices:
                    continue
                delta = getattr(chunk.choices[0].delta, "content", None)
                if delta:
                    yield delta
        except Exception as e:  # noqa: BLE001
            raise self._wrap(e) from e


class ExperientialAdapter(OpenAICompatAdapter):
    """Experiential Labs — an OpenAI-compatible model gateway with a FIXED endpoint.

    Wire protocol is identical to the generic adapter (Chat Completions + GET
    /v1/models), so list_models/complete/stream are inherited unchanged. The base
    URL is fixed, so the user supplies only their `xpl_` key; _client injects the
    endpoint when none is stored (needs_base_url is False ⇒ base_url comes as None).
    """
    key = "experientiallabs"
    label = "Experiential Labs"
    needs_base_url = False
    default_base_url = "https://api.experientiallabs.ai/v1"

    def _client(self, api_key: str, base_url: str | None) -> OpenAI:
        return super()._client(api_key, base_url or self.default_base_url)

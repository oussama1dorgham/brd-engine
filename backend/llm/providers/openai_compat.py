"""Adapter for any OpenAI-compatible provider (OpenAI, Groq, Together, Fireworks,
DeepInfra, Mistral, xAI, Perplexity, DeepSeek, Google's OpenAI-compat endpoint,
local Ollama/LM Studio/vLLM, OpenRouter, …). The user supplies base_url + key."""
from __future__ import annotations

import logging
from typing import Iterator

from openai import OpenAI

from backend.llm import free_router
from backend.llm.client import describe_error
from backend.llm.providers.base import ProviderError

log = logging.getLogger(__name__)

VERDICT_MSG = ("The selected model returned a content-safety verdict instead of an answer — it is a "
               "moderation model, not a chat model. Pick another model.")
DAILY_LIMIT_MSG = ("Your OpenRouter account has used today's free-model requests (50/day without credits; "
                   "1000/day after adding $10). Try again after midnight UTC, add credits, or pick a paid model.")
NO_FREE_MODEL_MSG = ("No free model could answer right now (they are busy or unavailable). Try again in a "
                     "minute, or pick a specific model.")


class OpenAICompatAdapter:
    key = "openai"
    label = "OpenAI-compatible"
    needs_base_url = True
    default_base_url = "https://api.openai.com/v1"

    def _client(self, api_key: str, base_url: str | None) -> OpenAI:
        return OpenAI(api_key=api_key, base_url=base_url, timeout=60, max_retries=0)

    def _wrap(self, e: Exception) -> ProviderError:
        raw = str(getattr(e, "body", None) or e)
        return ProviderError(describe_error(e), getattr(e, "status_code", None), raw=raw)

    def _extra(self, base_url: str | None, *, reasoning_off: bool = True) -> dict:
        """OpenRouter-only: disable reasoning so free 'thinking' models don't spend
        their whole completion budget on hidden chain-of-thought and return EMPTY
        content (finish_reason=length, 0 chars). Scoped to openrouter.ai so it's never
        sent to OpenAI/other providers that would reject an unknown field. A model whose
        reasoning is MANDATORY rejects that (400) — it is retried with low-effort, hidden
        reasoning instead (`reasoning_off=False`)."""
        if free_router.is_openrouter(base_url):
            r = {"enabled": False} if reasoning_off else {"effort": "low", "exclude": True}
            return {"extra_body": {"reasoning": r}}
        return {}

    def list_models(self, api_key: str, base_url: str | None) -> list[str]:
        try:
            resp = self._client(api_key, base_url).models.list()
        except Exception as e:  # noqa: BLE001
            raise self._wrap(e) from e
        return sorted({m.id for m in getattr(resp, "data", []) if getattr(m, "id", None)})

    # --- one call to one model ------------------------------------------------------------

    def _complete_one(self, api_key, base_url, model, messages, temperature, max_tokens) -> str:
        def call(reasoning_off: bool, budget: int):
            return self._client(api_key, base_url).chat.completions.create(
                model=model, messages=messages, temperature=temperature, max_tokens=budget,
                **self._extra(base_url, reasoning_off=reasoning_off))
        try:
            try:
                resp = call(True, max_tokens)
            except Exception as e:  # noqa: BLE001
                if "Reasoning is mandatory" not in str(getattr(e, "body", None) or e):
                    raise
                resp = call(False, max(max_tokens, 2000))       # room for its reasoning too
        except Exception as e:  # noqa: BLE001
            raise self._wrap(e) from e
        ch = getattr(resp, "choices", None)
        if not ch:
            return ""
        msg = getattr(ch[0], "message", None)
        return (getattr(msg, "content", None) or "") if msg else ""

    def _stream_one(self, api_key, base_url, model, messages, temperature, max_tokens) -> Iterator[str]:
        def call(reasoning_off: bool, budget: int):
            return self._client(api_key, base_url).chat.completions.create(
                model=model, messages=messages, temperature=temperature, max_tokens=budget, stream=True,
                **self._extra(base_url, reasoning_off=reasoning_off))
        try:
            try:
                s = call(True, max_tokens)
            except Exception as e:  # noqa: BLE001
                if "Reasoning is mandatory" not in str(getattr(e, "body", None) or e):
                    raise
                s = call(False, max(max_tokens, 2000))
            for chunk in s:
                if not chunk.choices:
                    continue
                delta = getattr(chunk.choices[0].delta, "content", None)
                if delta:
                    yield delta
        except Exception as e:  # noqa: BLE001
            raise self._wrap(e) from e

    @staticmethod
    def _peek(gen: Iterator[str], n: int = 80) -> tuple[str, Iterator[str]]:
        """Read the first ~n characters of a stream (or all of a short one) before anything
        is shown, so a verdict / empty reply can be caught. Returns (head, rest)."""
        head = ""
        for piece in gen:
            head += piece
            if len(head.strip()) >= n:
                break
        return head, gen

    # --- public surface ---------------------------------------------------------------------

    def complete(self, api_key, base_url, model, messages, *, temperature, max_tokens) -> str:
        if free_router.is_router(model, base_url):
            return self._router(api_key, base_url, messages, temperature, max_tokens, stream=False)
        text = self._complete_one(api_key, base_url, model, messages, temperature, max_tokens)
        if free_router.is_verdict(text):
            raise ProviderError(VERDICT_MSG, 422)
        return text

    def stream(self, api_key, base_url, model, messages, *, temperature, max_tokens) -> Iterator[str]:
        if free_router.is_router(model, base_url):
            yield from self._router(api_key, base_url, messages, temperature, max_tokens, stream=True)
            return
        gen = self._stream_one(api_key, base_url, model, messages, temperature, max_tokens)
        if not free_router.is_openrouter(base_url):
            yield from gen                       # other providers: plain passthrough, as before
            return
        head = ""                                # OpenRouter: hold the first ~80 chars back so a
        try:                                     # moderation verdict is never shown as an answer
            for piece in gen:
                head += piece
                if len(head.strip()) >= 80:
                    break
        except ProviderError:
            if head:
                yield head                       # keep a partial answer, as before this check
            raise
        if free_router.is_verdict(head):
            raise ProviderError(VERDICT_MSG, 422)
        if head:
            yield head
        yield from gen

    def _router(self, api_key, base_url, messages, temperature, max_tokens, *, stream: bool):
        """`openrouter/free`: up to MAX_ATTEMPTS curated free chat models, one at a time. A
        model that errors, returns nothing, or returns a moderation verdict is cooled down
        and the next is tried — before anything reaches the user. Once a stream has started
        showing text it is never switched mid-answer."""
        tried: list[str] = []
        last: ProviderError | None = None
        for model in free_router.candidates(base_url)[:free_router.MAX_ATTEMPTS]:
            tried.append(model)
            try:
                if not stream:
                    text = self._complete_one(api_key, base_url, model, messages, temperature, max_tokens)
                    if text.strip() and not free_router.is_verdict(text):
                        return text
                    free_router.cool(model, 24 * 3600 if text.strip() else 600)
                    continue
                head, rest = self._peek(self._stream_one(api_key, base_url, model, messages, temperature, max_tokens))
            except ProviderError as e:
                if free_router.account_limited(e.raw):
                    raise ProviderError(DAILY_LIMIT_MSG, 429, raw=e.raw) from e
                if free_router.account_error(e.status, e.raw):
                    raise                        # the key / account, not the model: say so, cool nothing
                free_router.cool(model, free_router.cooldown_for(e.status, e.raw))
                last = e
                continue
            if not head.strip() or free_router.is_verdict(head):
                free_router.cool(model, 24 * 3600 if head.strip() else 600)
                continue
            log.info("free router: answered by %s (tried %s)", model, tried)
            return self._chain(head, rest)
        log.warning("free router: no usable reply from %s (last error: %s)", tried, last)
        # 503, not the last model's status: the router already tried several models, so the
        # engine must not re-run it on a 429 (that multiplied 3 calls into 9)
        raise ProviderError(NO_FREE_MODEL_MSG, 503, raw=last.raw if last else "")

    @staticmethod
    def _chain(head: str, rest: Iterator[str]) -> Iterator[str]:
        yield head
        yield from rest


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

"""LLM client for generation, routed through OpenRouter (OpenAI-compatible).

OpenRouter exposes an OpenAI-style Chat Completions API, so we use the openai
SDK pointed at the OpenRouter base URL. GEN_MODEL in .env selects the model.

Free models share a rate-limited upstream pool, so chat() retries on 429 with
backoff (honoring Retry-After when present).

    python -m backend.generate.llm          # connectivity ping
"""
from __future__ import annotations

import time

from openai import OpenAI, RateLimitError

from ..config import settings

_client: OpenAI | None = None

# Optional attribution headers OpenRouter uses for its dashboard/leaderboards.
_HEADERS = {"HTTP-Referer": "http://localhost", "X-Title": "BRD Retrieval Engine"}


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=settings.require_openrouter(),
            base_url=settings.require_base_url(),
        )
    return _client


def chat(messages: list[dict], model: str | None = None, temperature: float = 0.0,
         max_tokens: int = 600, stream: bool = False, max_retries: int = 5):
    """Create a chat completion, retrying on upstream 429 rate limits."""
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
            return client().chat.completions.create(**kwargs)
        except RateLimitError as err:
            last_err = err
            retry_after = 5
            try:
                header = err.response.headers.get("retry-after")
                if header:
                    retry_after = int(header)
            except Exception:
                pass
            time.sleep(min(retry_after, 20) + attempt * 3)
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

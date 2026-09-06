"""Common surface every provider adapter implements.

Messages are OpenAI-style: a list of {"role": "system"|"user"|"assistant",
"content": str}. Adapters translate to their provider's shape (e.g. Anthropic
lifts system out of the array). Text is normalized both ways: complete() returns
a plain string; stream() yields text pieces. Failures raise ProviderError with a
user-facing message, mapped by describe() in each adapter.
"""
from __future__ import annotations

from typing import Iterator, Protocol, runtime_checkable


class ProviderError(Exception):
    """A provider call failed. `message` is safe to show the user; `status` is the
    HTTP status when known (used to tailor the message: 429/402/401/404/…)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@runtime_checkable
class Adapter(Protocol):
    key: str                      # stable id stored in the DB, e.g. "anthropic"
    label: str                    # UI label, e.g. "Anthropic (Claude)"
    needs_base_url: bool          # user must supply a base_url (custom OpenAI-compatible)
    default_base_url: str | None  # prefilled in the UI; None if the provider is fixed

    def list_models(self, api_key: str, base_url: str | None) -> list[str]:
        """Model ids this key can use. Raises ProviderError on failure."""
        ...

    def complete(self, api_key: str, base_url: str | None, model: str,
                 messages: list[dict], *, temperature: float, max_tokens: int) -> str:
        """One-shot completion → text. Raises ProviderError on failure."""
        ...

    def stream(self, api_key: str, base_url: str | None, model: str,
               messages: list[dict], *, temperature: float, max_tokens: int) -> Iterator[str]:
        """Streaming completion → yields text pieces. Raises ProviderError on failure."""
        ...


def split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """Split OpenAI-style messages into (system_text, non_system_messages) for
    providers (Anthropic, Gemini) that carry the system prompt separately."""
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    rest = [m for m in messages if m.get("role") != "system"]
    return system, rest

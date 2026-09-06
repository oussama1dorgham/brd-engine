"""Condense a follow-up question into a standalone search query.

Turn 2+ of a conversation is often elliptical ("what protocol does that use?").
Retrieval needs a self-contained query, so we rewrite the follow-up using the
recent history. Turn 1 (no history) is returned unchanged — no LLM call.
"""
from __future__ import annotations

from . import engine
from ..providers.base import ProviderError

CONDENSE_SYS = (
    "You rewrite the user's latest message into a single, self-contained search query. "
    "Carry over the SPECIFIC subject under discussion — named entities, the particular use case "
    "or requirement ID (e.g. UC-C01), or the case the user is asking about — and resolve references "
    "('this', 'it', 'these roles', 'that case') to it. Keep it specific, never generic. "
    "Output ONLY the rewritten query text — no quotes, no preamble, no explanation."
)


def condense(history: list[dict], question: str, *, provider: str | None = None,
             api_key: str | None = None, base_url: str | None = None, model: str | None = None) -> str:
    if not history:
        return question
    transcript = "\n".join(f"{h['role']}: {h['content']}" for h in history[-6:])
    try:
        out = engine.complete_chat(
            [
                {"role": "system", "content": CONDENSE_SYS},
                {"role": "user", "content": (
                    f"Conversation so far:\n{transcript}\n\n"
                    f"Latest question: {question}\n\nStandalone query:"
                )},
            ],
            temperature=0.0, max_tokens=80,
            provider=provider, api_key=api_key, base_url=base_url, model=model,
        ).strip().strip('"').strip()
    except ProviderError:
        return question   # condensation is best-effort — let the main answer surface any error
    return out or question   # fall back to the raw question on an empty/bad response

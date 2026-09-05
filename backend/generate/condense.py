"""Condense a follow-up question into a standalone search query.

Turn 2+ of a conversation is often elliptical ("what protocol does that use?").
Retrieval needs a self-contained query, so we rewrite the follow-up using the
recent history. Turn 1 (no history) is returned unchanged — no LLM call.
"""
from __future__ import annotations

from .llm import chat, message_text

CONDENSE_SYS = (
    "You rewrite the user's latest message into a single, self-contained search query. "
    "Carry over the SPECIFIC subject under discussion — named entities, the particular use case "
    "or requirement ID (e.g. UC-C01), or the case the user is asking about — and resolve references "
    "('this', 'it', 'these roles', 'that case') to it. Keep it specific, never generic. "
    "Output ONLY the rewritten query text — no quotes, no preamble, no explanation."
)


def condense(history: list[dict], question: str) -> str:
    if not history:
        return question
    transcript = "\n".join(f"{h['role']}: {h['content']}" for h in history[-6:])
    resp = chat(
        [
            {"role": "system", "content": CONDENSE_SYS},
            {"role": "user", "content": (
                f"Conversation so far:\n{transcript}\n\n"
                f"Latest question: {question}\n\nStandalone query:"
            )},
        ],
        temperature=0.0,
        max_tokens=80,
    )
    out = message_text(resp).strip().strip('"').strip()
    return out or question   # fall back to the raw question on an empty/bad response

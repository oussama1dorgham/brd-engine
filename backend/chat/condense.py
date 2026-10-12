"""Condense a follow-up question into a standalone search query.

Turn 2+ of a conversation is often elliptical ("what protocol does that use?").
Retrieval needs a self-contained query, so we rewrite the follow-up using the
recent history. Turn 1 (no history) is returned unchanged — no LLM call.
"""
from __future__ import annotations

import re

from backend.llm import engine
from backend.llm.providers.base import ProviderError

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
    # fall back to the raw question on an empty / unusable response — a rewrite that isn't a
    # query must never become the search ("User Safety: safe" from a moderation model did)
    return out if usable_rewrite(out, question, transcript) else question


# leaked reasoning: "We need to …", "The user is asking …", "Let me …" — but "The user roles in
# the payments BRD" is a fine query, so "the user" counts only before a reasoning verb
_LEAK = re.compile(r"^(?:okay|ok|alright|so)?[,.\s]*(?:we need to|let me|i need to|i will|i'll|thinking process"
                   r"|first,|to rewrite|here is|here's|rewritten query"
                   r"|the user (?:is asking|asks|asked|wants|wanted|would like|is requesting|requests))", re.I)
_WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
_FILLER = frozenset("what which when where does this that these those with from have about there their "
                    "they them then than into your some more also only such each".split())
_ARABIC = re.compile(r"[؀-ۿ]")


def usable_rewrite(out: str, question: str, context: str = "") -> bool:
    """Is `out` a usable standalone search query for `question`? Not empty, not a safety
    verdict, not leaked reasoning or an essay, and about the conversation: it shares at least
    one content word with the question or the recent turns (skipped when the rewrite is in
    another script than the question — an English query for an Arabic question is fine)."""
    from backend.llm.free_router import is_verdict
    out = (out or "").strip()
    if not out or is_verdict(out) or _LEAK.match(out):
        return False
    if len(out) > max(400, 3 * len(question)):
        return False
    if bool(_ARABIC.search(out)) != bool(_ARABIC.search(question)):
        return True
    known = {w.lower() for w in _WORD.findall(f"{question} {context}")} - _FILLER
    return any(w.lower() in known for w in _WORD.findall(out))

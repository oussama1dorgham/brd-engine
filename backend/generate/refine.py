"""Refinement layer — reduce ungrounded answers.

FRONT (deterministic): route greetings/small-talk away from the RAG path so the
model never fabricates an answer to a non-question.

BACK (grounding): a cheap always-on guard that requires an answer to cite a
source or abstain, plus a GATED LLM verifier (REFINE_VERIFY=1) that strips
unsupported claims — best enabled on a strong model.
"""
from __future__ import annotations

import re

from ..config import settings
from .answer import ABSTAIN, SYSTEM, _sources_block, parse_citations
from . import engine

_GREETING = re.compile(
    r"^\s*(hi|hello|hey|yo|thanks|thank you|good (morning|evening|afternoon)|"
    r"مرحبا|أهلا|اهلا|السلام عليكم|شكرا|شكراً)\b",
    re.IGNORECASE,
)

SMALLTALK_REPLY = (
    "Hi! I answer questions grounded in the Business Requirements Document — "
    "try asking about its objectives, use cases, roles, permissions, or integrations."
)


def refine_query(question: str) -> tuple[str, str]:
    """Return (kind, payload).

    kind == 'smalltalk' -> payload is a canned reply (skip retrieval + generation)
    kind == 'answer'    -> payload is the normalized query to run through the pipeline
    """
    q = re.sub(r"\s+", " ", question).strip()
    if _GREETING.match(q) and len(q) <= 40:
        return "smalltalk", SMALLTALK_REPLY
    return "answer", q


def is_grounded(answer: str, n_sources: int) -> bool:
    """Grounded == the answer abstains, or cites at least one valid source."""
    a = answer.strip()
    if not a or a == ABSTAIN or ABSTAIN in a:
        return True
    return len(parse_citations(a, n_sources)) > 0


def verify_answer(question: str, answer: str, chunks: list[dict], *, provider: str | None = None,
                  api_key: str | None = None, base_url: str | None = None, model: str | None = None) -> str:
    """GATED second pass: keep only claims supported by the cited SOURCES.

    No-op unless REFINE_VERIFY is enabled (it costs an extra LLM call).
    """
    if not settings.refine_verify or not chunks:
        return answer
    prompt = (
        "Review the DRAFT answer against the numbered SOURCES. Remove any sentence not "
        "supported by a cited source, keep the inline [n] citations, and add no new information. "
        f'If nothing in the draft is supported, reply with exactly: "{ABSTAIN}".\n\n'
        f"QUESTION: {question}\n\nSOURCES:\n{_sources_block(chunks)}\n\n"
        f"DRAFT:\n{answer}\n\nVERIFIED ANSWER:"
    )
    revised = engine.complete_chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        temperature=0.0, max_tokens=700,
        provider=provider, api_key=api_key, base_url=base_url, model=model,
    ).strip()
    return revised or answer

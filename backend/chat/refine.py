"""Refinement layer — reduce ungrounded answers.

FRONT (deterministic): route greetings/small-talk away from the RAG path so the
model never fabricates an answer to a non-question.

BACK (grounding): a cheap always-on guard that requires an answer to cite a
source or abstain, plus a GATED LLM verifier (REFINE_VERIFY=1) that strips
unsupported claims — best enabled on a strong model.
"""
from __future__ import annotations

import re

from backend.core.config import settings
from backend.chat.answer import ABSTAIN, SYSTEM, _sources_block, parse_citations
from backend.llm import engine

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


_WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
_FILLER = frozenset("what which when where does this that these those with from have about there their they "
                    "them then than into your some more also only such each based provided sources source "
                    "following include includes including would should could".split())
_MIN_WORDS, _MIN_SUPPORT = 3, 0.7


def _content_words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)} - _FILLER


def attach_citations(answer: str, chunks: list[dict]) -> str:
    """Cite an uncited answer deterministically: each line whose content words are mostly
    (≥70 %, at least 3 words) found in one retrieved chunk gets that chunk's [n]. Weak models
    often answer from the SOURCES verbatim but forget the markers; without this the grounding
    guard re-asks and a flaky retry abstains, discarding a correct answer. A line that no single
    source supports stays uncited, so this never vouches for anything the sources don't say."""
    vocab = [_content_words(c.get("text") or "") for c in chunks]
    out = []
    for line in answer.split("\n"):
        words = _content_words(line)
        if len(words) >= _MIN_WORDS and not parse_citations(line, len(chunks)):
            score, n = max(((len(words & v) / len(words), i) for i, v in enumerate(vocab, 1)), default=(0, 0))
            if score >= _MIN_SUPPORT:
                stripped = line.rstrip()
                line = f"{stripped} [{n}]{line[len(stripped):]}"
        out.append(line)
    return "\n".join(out)


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
        temperature=0.0, max_tokens=settings.gen_max_tokens,
        provider=provider, api_key=api_key, base_url=base_url, model=model,
    ).strip()
    # a verifier that drops every citation (or replies with something that is not an answer,
    # e.g. a safety verdict) has not verified anything — keep the draft rather than lose it
    return revised if revised and is_grounded(revised, len(chunks)) else answer

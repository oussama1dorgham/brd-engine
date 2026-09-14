"""Generate BRD-specific starter questions from a project's own chunks.

The UI shows suggested questions the moment a BRD is selected. Instead of static
prompts, we sample the document's chunks and ask the LLM for a few concise,
answerable questions. Results are cached per project (BRD content is static until
re-ingested), so it's one generation per BRD; `invalidate()` clears the cache on
re-upload or delete.
"""
from __future__ import annotations

import json
import logging
import re
import threading

from ..db import pool
from .llm import chat, message_text

log = logging.getLogger("brd.suggest")

_cache: dict[tuple[int, str], list[str]] = {}   # keyed by (owner_id, project)
_lock = threading.Lock()

# Shown if the BRD has no chunks or generation fails — never leaves the user empty.
_FALLBACK = [
    "Give me an overview of this document.",
    "What are the main requirements or user stories?",
    "Which roles or actors are involved?",
    "What are the key use cases or scenarios?",
]


def _sample_chunks(project: str, owner_id: int, limit: int = 16) -> list[str]:
    """Chunks spread across this user's BRD.

    Sampling only the leading chunks skews every question toward the intro/scope.
    We take the first few chunks (they frame the document) and then evenly stride
    through the rest so requirements, actors, and edge cases buried deeper in the
    BRD get a chance to seed a question too.
    """
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """select c.text from brd_chunk c
               join brd_document d on d.id = c.document_id
               where d.project = %s and d.owner_id = %s and d.status = 'ready'
               order by c.ordinal""",
            (project, owner_id),
        )
        rows = [r[0] for r in cur.fetchall()]

    if len(rows) <= limit:
        return rows
    head = max(1, limit // 4)                       # keep the opening context intact
    picked = rows[:head]
    rest, remaining = rows[head:], limit - head
    step = len(rest) / remaining                    # even stride over the remainder
    picked.extend(rest[int(i * step)] for i in range(remaining))
    return picked


def _parse_questions(text: str) -> list[str]:
    text = (text or "").strip()
    m = re.search(r"\[.*\]", text, re.DOTALL)     # prefer a JSON array
    if m:
        try:
            arr = json.loads(m.group(0))
            qs = [str(x).strip() for x in arr if str(x).strip()]
            if qs:
                return qs
        except Exception:  # noqa: BLE001
            pass
    # fallback: one question per line, stripped of bullets/numbering/quotes
    out: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip('"')
        if line.endswith("?"):
            out.append(line)
    return out


def generate_starters(project: str, owner_id: int, n: int = 4, refresh: bool = False) -> list[str]:
    """Return ~n starter questions grounded in the BRD (cached per owner+project)."""
    key = (owner_id, project)
    if not refresh:
        with _lock:
            if key in _cache:
                return _cache[key]

    chunks = _sample_chunks(project, owner_id)
    if not chunks:
        return _FALLBACK

    context = "\n\n".join(chunks)[:5000]
    messages = [
        {"role": "system", "content": (
            "You propose starter questions a stakeholder could ask about a specific "
            "Business Requirements Document. Good questions are concrete and reference "
            "the document's actual subject matter — a named feature, role, rule, or "
            "constraint — not generic prompts like 'give me an overview' that would fit "
            "any document. Every question must be answerable from the document's own content."
        )},
        {"role": "user", "content": (
            f"Excerpts from a BRD:\n\n{context}\n\n"
            f"Suggest {n} distinct, specific questions (max ~14 words each) a reader might "
            f"ask about THIS document. Make each cover a different aspect — e.g. a specific "
            f"requirement, an actor/role, a business rule or constraint, or an edge case — "
            f"and name the concrete thing being asked about rather than staying vague. "
            f"Match the document's language. Return ONLY a JSON array of strings — no "
            f"numbering, no extra text."
        )},
    ]
    try:
        text = message_text(chat(messages, temperature=0.3, max_tokens=300))
        qs = _parse_questions(text)[:n]
    except Exception as e:  # noqa: BLE001
        log.warning("starter generation failed for %s: %s", project, e)
        qs = []

    if not qs:
        qs = _FALLBACK
    with _lock:
        _cache[key] = qs
    return qs


def invalidate(project: str, owner_id: int) -> None:
    """Drop cached questions for a user's project (call on re-ingest or delete)."""
    with _lock:
        _cache.pop((owner_id, project), None)

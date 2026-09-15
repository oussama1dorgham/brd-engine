"""Short human titles for requirement chunks — friendly citation labels.

A requirement chunk is often a heading-less blob (or a flattened table), so its
citation would otherwise fall back to raw text or an internal id. This derives a
short, human title per chunk via the LLM, ONCE, cached by content hash — a title is a
pure function of the chunk's text, so repeats are free and a text edit self-invalidates
(new hash → new key). Chunks that already carry a real req_id/section reuse that instead
of spending an LLM call. Fail-open: any error yields no title and the caller keeps its
existing fallback label.
"""
from __future__ import annotations

import json
import logging
import re

from .. import cache
from ..config import settings
from ..db import pool
from . import engine
from .change_agent import GenRoute

log = logging.getLogger("brd.reqtitles")

_NS = "reqtitle"
_MAXTEXT = 700          # per-requirement text budget in the prompt
_BATCH = 12             # requirements per LLM call


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


_PROMPT = """You label requirements from a Business Requirements Document. For EACH numbered
requirement below, write a short, specific title (4–8 words) naming what it is about, in the
SAME language as the requirement. No numbering, no quotes, no trailing punctuation.

REQUIREMENTS:
{items}

Return ONLY a JSON array of {n} strings, in the same order — nothing else."""


def _generate(texts: list[str], route: GenRoute | None) -> list[str]:
    r = route or GenRoute()
    items = "\n".join(f"{i + 1}. {_norm(t)[:_MAXTEXT]}" for i, t in enumerate(texts))
    prompt = _PROMPT.replace("{items}", items).replace("{n}", str(len(texts)))
    out = engine.complete_chat(
        [{"role": "user", "content": prompt}],
        provider=r.provider, api_key=r.api_key, base_url=r.base_url, model=r.model,
        temperature=0.0, max_tokens=60 * len(texts) + 200,
    )
    m = re.search(r"\[.*\]", out or "", re.S)
    if not m:
        return [""] * len(texts)
    try:
        arr = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return [""] * len(texts)
    titles = [_norm(str(x)) for x in arr] if isinstance(arr, list) else []
    return (titles + [""] * len(texts))[:len(texts)]   # align to input length


def titles_for(owner_id: int, project: str, chunk_ids: list[int],
               route: GenRoute | None = None) -> dict[int, str]:
    """Map {chunk_id -> short title} for a project's chunks. Cached per content hash;
    real req_id/section reused verbatim; misses generated in batches. Never raises."""
    if not chunk_ids:
        return {}
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "select c.id, c.content_hash, c.text, c.req_id, c.section from brd_chunk c "
                "join brd_document d on d.id = c.document_id "
                "where d.owner_id = %s and d.project = %s and c.id = any(%s)",
                (owner_id, project, list(chunk_ids)),
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("title lookup failed: %s", e)
        return {}

    model = (route.model if route else None) or settings.gen_model or ""
    out: dict[int, str] = {}
    misses: list[tuple[int, str, str]] = []   # (cid, content_hash, text)
    for cid, chash, text, req_id, section in rows:
        key = [chash, model]
        hit = cache.get(_NS, key)
        if hit:
            out[cid] = hit
            continue
        real = _norm(req_id) or _norm(section)
        if real:                                # already labelled — no LLM needed
            cache.set(_NS, key, real, owner_id=owner_id, project=project)
            out[cid] = real
        else:
            misses.append((cid, chash, text))

    for i in range(0, len(misses), _BATCH):
        batch = misses[i:i + _BATCH]
        try:
            titles = _generate([t for _, _, t in batch], route)
        except Exception as e:  # noqa: BLE001 — fail-open, keep the caller's fallback
            log.warning("title generation failed: %s", e)
            continue
        for (cid, chash, _t), title in zip(batch, titles):
            if title:
                cache.set(_NS, [chash, model], title, owner_id=owner_id, project=project)
                out[cid] = title
    return out

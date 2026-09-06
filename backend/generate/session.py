"""Multi-turn chat session: condense -> retrieve -> stream grounded answer.

Keeps conversation history so follow-ups resolve, reuses the grounding prompt
and citation validation from answer.py, and streams tokens as they arrive.
The same ChatSession backs the Chainlit UI in 3.4 (pass an on_token callback).

Demo (scripted multi-turn conversation):
    python -m backend.generate.session
"""
from __future__ import annotations

import logging
import re

from .. import cache
from ..config import settings
from ..retrieve.retriever import retrieve
from .answer import ABSTAIN, parse_citations, _sources_block, SYSTEM
from .condense import condense
from openai import RateLimitError

from .history_store import create_conversation, save_message
from .llm import chat, message_text
from .refine import is_grounded, refine_query, verify_answer


class GenerationCanceled(BaseException):
    """Raised to abort an in-flight answer WITHOUT persisting it (user hit Stop).

    Deliberately a BaseException so the broad ``except Exception`` guards around the
    optional steps (grounding re-ask, verify) don't swallow it — a cancel must
    propagate out of ask() before _finish() ever runs, so nothing is saved.
    """

log = logging.getLogger("brd.session")

# Demonstratives/pronouns that signal a follow-up depends on prior context (EN + AR).
_UNRESOLVED_REF = re.compile(r"\b(this|that|these|those|it|its)\b|هذه|هذا|هؤلاء|تلك|ذلك|الحالة", re.IGNORECASE)


class ChatSession:
    def __init__(self, project: str | None = None, k: int = 5,
                 persist: bool = False, user_id: str | None = None,
                 owner_id: int | None = None, gen_model: str | None = None,
                 gen_key: str | None = None, gen_base_url: str | None = None):
        self.project = project
        self.k = k
        self.persist = persist
        self.user_id = user_id
        self.owner_id = owner_id       # app_user.id — scopes retrieval to this user's BRDs
        # Bring-your-own-key generation: the user's provider key/base_url + chosen
        # model. All None => fall back to the system GEN_MODEL / OpenRouter.
        self.gen_model = gen_model
        self.gen_key = gen_key
        self.gen_base_url = gen_base_url
        self.conversation_id: int | None = None
        self.history: list[dict] = []

    def ask(self, question: str, on_token=None, on_standalone=None, should_cancel=None) -> dict:
        # should_cancel() is polled at safe points so the user's Stop aborts the turn
        # before anything is persisted. _bail() raises out past every optional guard.
        def _bail() -> None:
            if should_cancel and should_cancel():
                raise GenerationCanceled()

        # FRONT refinement: route greetings/small-talk away from the RAG path,
        # so the model never fabricates an answer to a non-question.
        kind, payload = refine_query(question)
        if kind == "smalltalk":
            if on_standalone:
                on_standalone(question)
            if on_token:
                on_token(payload)
            return self._finish(question, question, payload, [], sources=[])
        question = payload  # normalized query

        standalone = condense(self.history, question, api_key=self.gen_key,
                              base_url=self.gen_base_url, model=self.gen_model)
        if on_standalone:
            on_standalone(standalone)
        # Robustness net: if condensation left an unresolved reference ("this case", "هذه الحالة")
        # — common with weak models — augment the RETRIEVAL query with the last user turn.
        retrieval_query = standalone
        if self.history and standalone.strip() == question.strip() and _UNRESOLVED_REF.search(question):
            prev_user = next((h["content"] for h in reversed(self.history) if h["role"] == "user"), "")
            if prev_user:
                retrieval_query = f"{prev_user}\n{question}"
        _bail()
        chunks = retrieve(retrieval_query, owner_id=self.owner_id, k_final=self.k,
                          project=self.project, use_rerank=True)
        _bail()

        # ANSWER CACHE: keyed on the standalone question + the ordered ids of the
        # retrieved chunks + GEN_MODEL. A hit replays the stored answer and skips
        # generation + grounding + verify. Corpus changes shift the chunk ids (and
        # are busted on ingest), and a model swap changes GEN_MODEL — so either
        # self-invalidates. History is intentionally NOT in the key: condense() has
        # already folded it into the standalone question.
        ans_parts = [standalone, [c["chunk_id"] for c in chunks], self.gen_model or settings.gen_model]
        cached = cache.get(cache.ANSWER, ans_parts)
        if cached is not None:
            if on_token:
                on_token(cached["answer"])
            return self._finish(question, standalone, cached["answer"], chunks, sources=None)

        # Include recent turns so the model can resolve references; system prompt keeps grounding.
        messages = [{"role": "system", "content": SYSTEM}]
        messages += [{"role": h["role"], "content": h["content"]} for h in self.history[-4:]]
        messages.append(
            {"role": "user", "content": f"Question: {standalone}\n\nSOURCES:\n{_sources_block(chunks)}"}
        )
        text = self._generate(messages, on_token, should_cancel)
        generation_ok = bool(text)  # never cache the transient-failure fallback below
        if not text:
            text = "I couldn't produce an answer just now — please try again."

        # BACK refinement (grounding): if it cited nothing, re-ask once to cite or abstain.
        if chunks and not is_grounded(text, len(chunks)):
            strict = messages + [{"role": "user", "content": (
                "Your previous answer stated things without citing the SOURCES. Re-answer using ONLY the "
                f'numbered SOURCES, with an inline [n] citation for each claim, or reply exactly: "{ABSTAIN}".'
            )}]
            try:
                retry = self._generate(strict, None, should_cancel)
            except Exception:  # noqa: BLE001 — an optional re-ask must not discard a real answer
                retry = ""
            if retry and is_grounded(retry, len(chunks)):
                text = retry
        # BACK refinement (gated): LLM verification strips unsupported claims (REFINE_VERIFY=1).
        # Fail-open: a provider error on this OPTIONAL extra call must never discard a good
        # answer — BYOK keys can 402 (no credits) / 404 (batch-only model) / exhaust 429s here.
        try:
            text = verify_answer(standalone, text, chunks, api_key=self.gen_key,
                                 base_url=self.gen_base_url, model=self.gen_model)
        except Exception:  # noqa: BLE001
            log.warning("verify_answer failed; keeping the unverified answer", exc_info=True)

        # Cache the final, post-verify answer — but only a real one. A transient
        # provider failure ("couldn't produce an answer") must never be memoized.
        if generation_ok:
            cache.set(cache.ANSWER, ans_parts, {"answer": text},
                      owner_id=self.owner_id, project=self.project)

        _bail()   # last check: a Stop between the final token and persistence still wins
        return self._finish(question, standalone, text, chunks, sources=None)

    def _finish(self, question: str, standalone: str, text: str, chunks: list[dict], sources) -> dict:
        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": text})
        if sources is None:
            cited = parse_citations(text, len(chunks))
            sources = [{"n": n, **chunks[n - 1]} for n in cited]
        if self.persist:
            try:  # persistence must never break the chat
                if self.conversation_id is None:
                    self.conversation_id = create_conversation(self.user_id, self.project)
                save_message(self.conversation_id, "user", question)
                save_message(self.conversation_id, "assistant", text,
                             cited_chunk_ids=[s["chunk_id"] for s in sources if "chunk_id" in s])
            except Exception as e:  # noqa: BLE001
                log.warning("persist failed: %s: %s", type(e).__name__, e)
        return {"standalone": standalone, "answer": text, "sources": sources, "chunks": chunks}

    def _generate(self, messages: list[dict], on_token, should_cancel=None) -> str:
        parts: list[str] = []
        try:
            for chunk in chat(messages, temperature=0.0, max_tokens=600, stream=True,
                              api_key=self.gen_key, base_url=self.gen_base_url, model=self.gen_model):
                if should_cancel and should_cancel():
                    raise GenerationCanceled()   # stop consuming tokens now (saves budget)
                if not chunk.choices:
                    continue
                delta = getattr(chunk.choices[0].delta, "content", None)
                if delta:
                    parts.append(delta)
                    if on_token:
                        on_token(delta)
        except RateLimitError:
            if not parts:
                raise          # no partial answer to salvage — surface the rate limit
        except Exception:      # other provider/stream hiccups shouldn't crash the chat
            pass
        text = "".join(parts).strip()
        if text:
            return text
        # Streaming returned nothing (flaky free pool) — retry once, non-streaming.
        try:
            text = message_text(chat(messages, temperature=0.0, max_tokens=600,
                                     api_key=self.gen_key, base_url=self.gen_base_url,
                                     model=self.gen_model)).strip()
        except RateLimitError:
            raise              # surface rate limits so the UI shows a clear error
        except Exception:
            return ""
        if text and on_token:
            on_token(text)
        return text


if __name__ == "__main__":
    import sys

    def writer(tok: str) -> None:
        sys.stdout.write(tok)
        sys.stdout.flush()

    session = ChatSession(project="payments")
    turns = [
        "How do staff sign in to the admin panel?",   # turn 1: standalone
        "What protocol does that use?",               # turn 2: needs 'that' resolved
        "Is a plain password allowed instead?",       # turn 3: needs context
    ]
    for i, q in enumerate(turns, 1):
        print(f"\n===== turn {i} =====")
        print(f"you: {q}")

        def show_standalone(s: str, _q: str = q) -> None:
            if s.strip() != _q.strip():
                print(f"     [condensed -> {s!r}]")
            print("bot: ", end="", flush=True)

        res = session.ask(q, on_token=writer, on_standalone=show_standalone)
        print()
        cited = ", ".join(f"[{s['n']}]{s['req_id'] or s['section']}" for s in res["sources"]) or "none"
        print(f"     cited: {cited}")

"""Central Voyage client with a free-tier budget, priority, and retry.

Without a payment method Voyage caps the account at VOYAGE_MAX_RPM requests/min AND
VOYAGE_MAX_TPM tokens/min (3 / 10K). Every embed/rerank call goes through `guarded()`,
which spends from ONE shared sliding-window budget tracking both limits:

  * priority="query"  — interactive work (chat query embeddings, rerank, a single
                        requirement edit). Always served first.
  * priority="ingest" — background document embedding. Never overtakes a waiting query,
                        and while chat has been active in the last `_QUERY_RECENT` seconds
                        it leaves VOYAGE_QUERY_RESERVE_RPM request slots free — so uploading
                        a BRD never stalls anyone's questions.

Pacing to the budget means 429s are rare; when one still happens the whole window backs
off. Transient failures (5xx, 503, connection errors, timeouts) retry with backoff and then
raise `VoyageUnavailable`; caller errors (bad key / bad request) raise immediately.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

import voyageai
from voyageai import error as verr

from backend.core.config import settings

log = logging.getLogger("brd.voyage")


class VoyageUnavailable(RuntimeError):
    """The Voyage embedding/rerank service is failing (transient) after retries."""


_client: voyageai.Client | None = None
_client_lock = threading.Lock()


def client() -> voyageai.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = voyageai.Client(api_key=settings.require_voyage(), max_retries=0, timeout=60)
    return _client


_WINDOW = 60.0
_QUERY_RECENT = 90.0          # chat counts as "active" for this long after a query call
_TRANSIENT = tuple(getattr(verr, n) for n in
                   ("ServerError", "ServiceUnavailableError", "APIConnectionError", "Timeout", "TryAgain")
                   if hasattr(verr, n))


class Budget:
    """Sliding-window RPM + TPM budget with query-over-ingest priority (thread-safe)."""

    def __init__(self, rpm: int, tpm: int, query_reserve: int, clock=time.monotonic):
        self.rpm, self.tpm, self.reserve = max(1, rpm), max(1, tpm), max(0, query_reserve)
        self.clock = clock
        self.calls: deque[tuple[float, int]] = deque()
        self.cond = threading.Condition()
        self.queries_waiting = 0
        self.last_query = -1e9
        self.blocked_until = 0.0            # set by a 429: nobody fires until then

    def _prune(self, now: float) -> None:
        while self.calls and now - self.calls[0][0] >= _WINDOW:
            self.calls.popleft()

    def _admissible(self, priority: str, tokens: int, now: float) -> bool:
        if now < self.blocked_until:
            return False
        if priority == "ingest" and self.queries_waiting:
            return False                                          # queries go first
        slots = self.rpm
        if priority == "ingest" and now - self.last_query < _QUERY_RECENT:
            slots = max(1, self.rpm - self.reserve)               # keep room for chat
        used_tokens = sum(t for _, t in self.calls)
        fits_tokens = used_tokens + tokens <= self.tpm or not self.calls   # an oversize call runs alone
        return len(self.calls) < slots and fits_tokens

    def _wait_hint(self, now: float) -> float:
        waits = [self.blocked_until - now]
        if self.calls:
            waits.append(_WINDOW - (now - self.calls[0][0]))
        return max(0.05, min(w for w in waits if w > 0) if any(w > 0 for w in waits) else 0.25)

    def acquire(self, priority: str = "query", tokens: int = 0) -> None:
        with self.cond:
            if priority == "query":
                self.queries_waiting += 1
            try:
                while True:
                    now = self.clock()
                    self._prune(now)
                    if self._admissible(priority, tokens, now):
                        self.calls.append((now, max(0, int(tokens))))
                        if priority == "query":
                            self.last_query = now
                        return
                    self.cond.wait(timeout=self._wait_hint(now))
            finally:
                if priority == "query":
                    self.queries_waiting -= 1
                self.cond.notify_all()

    def snapshot(self) -> dict:
        """What this process has used of the current one-minute window — for the UI. Voyage's
        API reports no remaining quota, so our own pacing ledger is the only live signal."""
        with self.cond:
            now = self.clock()
            self._prune(now)
            return {"rpm_limit": self.rpm, "rpm_used": len(self.calls),
                    "tpm_limit": self.tpm, "tpm_used": sum(t for _, t in self.calls),
                    "reserved_for_chat": self.reserve, "queries_waiting": self.queries_waiting,
                    "blocked_for": round(max(0.0, self.blocked_until - now), 1),
                    "window_resets_in": round(max(0.0, _WINDOW - (now - self.calls[0][0])) if self.calls else 0.0, 1)}

    def back_off(self, seconds: float) -> None:
        """A 429 arrived despite pacing: pause every caller for `seconds`."""
        with self.cond:
            self.blocked_until = max(self.blocked_until, self.clock() + seconds)
            self.cond.notify_all()


budget = Budget(settings.voyage_max_rpm, settings.voyage_max_tpm, settings.voyage_query_reserve_rpm)


def estimate_tokens(texts) -> int:
    """~4 chars/token — only used to reserve TPM before a call (Voyage reports the truth)."""
    if isinstance(texts, str):
        texts = [texts]
    return max(1, sum(len(t or "") for t in texts) // 4)


def guarded(fn, *args, priority: str = "query", est_tokens: int | None = None, **kwargs):
    """Call a Voyage SDK method within the shared budget, with retry on 429 / transient errors."""
    if est_tokens is None:
        est_tokens = estimate_tokens([a for a in args if isinstance(a, str)] +
                                     [x for a in args if isinstance(a, list) for x in a if isinstance(x, str)])
    transient = 0
    for attempt in range(6):
        budget.acquire(priority, est_tokens)
        try:
            return fn(*args, **kwargs)
        except verr.RateLimitError:
            wait = 20.0 * (attempt + 1)
            log.warning("Voyage 429 (attempt %d) — backing off %.0fs", attempt + 1, wait)
            budget.back_off(wait)
        except _TRANSIENT as err:
            transient += 1
            log.warning("Voyage transient error %s (attempt %d)", type(err).__name__, transient)
            if transient >= 4:
                raise VoyageUnavailable(
                    "The embedding service (Voyage) is temporarily unavailable. Please try again in a moment."
                ) from err
            time.sleep(2 ** transient)
    raise VoyageUnavailable("The embedding service (Voyage) kept rate-limiting; try again shortly.")

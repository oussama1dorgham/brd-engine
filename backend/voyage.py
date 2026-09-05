"""Central Voyage client with free-tier-friendly throttling and retry.

Without a payment method, Voyage caps the account at a few requests/minute.
Every embed/rerank call goes through `guarded()`, which paces requests under
VOYAGE_MAX_RPM and backs off on RateLimitError instead of crashing. Once a
payment method is added, raise VOYAGE_MAX_RPM in .env and calls speed up.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import voyageai
from voyageai.error import RateLimitError, ServerError

from .config import settings

log = logging.getLogger("brd.voyage")


class VoyageUnavailable(RuntimeError):
    """The Voyage embedding/rerank service is failing (transient 5xx) after retries."""

_client: voyageai.Client | None = None
_lock = threading.Lock()
_calls: list[float] = []
_MAX_RPM = int(os.getenv("VOYAGE_MAX_RPM", "3"))
_WINDOW = 60.0


def client() -> voyageai.Client:
    global _client
    if _client is None:
        _client = voyageai.Client(api_key=settings.require_voyage())
    return _client


def _throttle() -> None:
    """Block until firing another request keeps us within MAX_RPM per minute."""
    with _lock:
        now = time.monotonic()
        while _calls and now - _calls[0] > _WINDOW:
            _calls.pop(0)
        if len(_calls) >= _MAX_RPM:
            time.sleep(max(_WINDOW - (now - _calls[0]) + 0.5, 0.0))
            now = time.monotonic()
            while _calls and now - _calls[0] > _WINDOW:
                _calls.pop(0)
        _calls.append(time.monotonic())


def guarded(fn, *args, **kwargs):
    """Call a Voyage SDK method with throttling + backoff on rate limits and transient 5xx."""
    last_err: Exception | None = None
    server_errors = 0
    for attempt in range(5):
        _throttle()
        try:
            return fn(*args, **kwargs)
        except RateLimitError as err:
            last_err = err
            time.sleep(22 * (attempt + 1))
        except ServerError as err:
            last_err = err
            server_errors += 1
            log.warning("Voyage server error (attempt %d) — retrying", server_errors)
            if server_errors >= 3:
                break
            time.sleep(2 * server_errors)
    if isinstance(last_err, ServerError):
        raise VoyageUnavailable(
            "The embedding service (Voyage) is temporarily unavailable. Please try again in a moment."
        ) from last_err
    raise last_err  # type: ignore[misc]

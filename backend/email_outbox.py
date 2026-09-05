"""Durable email outbox with FIXED-interval retry.

Auth email is written to `email_outbox` first, then sent. The happy path fires an
immediate send in a daemon thread; any failure is left for a background sweeper
that retries at a FIXED interval (EMAIL_RETRY_INTERVAL_SECONDS, default 3 min —
deliberately not exponential, which is a poor UX for verification/reset mail) up
to EMAIL_MAX_ATTEMPTS, then dead-letters the row.

Scope is PER USER / RECIPIENT (authentication is per-user; no session exists yet
at signup/reset). Limits:
  * request limit ...... `max_attempts` per message (attempts++ at CLAIM time, so a
                         crash still counts and a poison message can't loop forever).
  * per-attempt count .. `next_attempt_at = now() + retry_interval` (fixed) on failure.
  * exhaustion cooldown  a dead-letter blocks new mail to that recipient for
                         EMAIL_RECIPIENT_COOLDOWN_SECONDS (no re-flooding).

Race-freedom: rows are claimed with a single atomic UPDATE that flips
status -> 'sending' guarded on the current status; the immediate attempt and the
sweeper (and multiple uvicorn workers) therefore can never double-send. A lease
(`locked_at`) reclaims rows abandoned by a crashed worker. Delivery is
at-least-once (a send that succeeds but crashes before commit may repeat — rare,
and harmless for verification/reset; true exactly-once would need a provider
idempotency key).
"""
from __future__ import annotations

import logging
import threading
import time

from .config import settings
from .db import pool
from .email_send import send_email

log = logging.getLogger("brd.email.outbox")

PENDING, SENDING, SENT, FAILED = "pending", "sending", "sent", "failed"

# columns returned by a claim, in order
_COLS = "id, recipient_email, subject, body, html, attempts, max_attempts"


def enqueue(recipient_email: str, subject: str, body: str, *,
            html: str | None = None, user_id: int | None = None,
            send_now: bool = True) -> int | None:
    """Queue one email (per user/recipient). Returns the row id, or None if the
    recipient is inside a post-failure cooldown or the write fails. Never raises."""
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "select 1 from email_outbox where recipient_email = %s and status = %s "
                "and failed_at > now() - make_interval(secs => %s) limit 1",
                (recipient_email, FAILED, settings.email_recipient_cooldown_seconds),
            )
            if cur.fetchone():
                log.warning("email to %s suppressed: within post-failure cooldown", recipient_email)
                return None
            cur.execute(
                "insert into email_outbox (user_id, recipient_email, subject, body, html, max_attempts) "
                "values (%s, %s, %s, %s, %s, %s) returning id",
                (user_id, recipient_email, subject, body, html, settings.email_max_attempts),
            )
            row_id = cur.fetchone()[0]
    except Exception:  # noqa: BLE001 — enqueue must not break the request
        log.exception("failed to enqueue email to %s", recipient_email)
        return None

    if send_now:
        threading.Thread(target=_try_send_now, args=(row_id,),
                         daemon=True, name=f"email-send-{row_id}").start()
    return row_id


def _claim_specific(row_id: int) -> tuple | None:
    """Atomically claim ONE pending, due row by id. Returns the row or None if
    another claimer already took it (status no longer 'pending')."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"update email_outbox set status = %s, locked_at = now(), attempts = attempts + 1 "
            f"where id = %s and status = %s and next_attempt_at <= now() "
            f"returning {_COLS}",
            (SENDING, row_id, PENDING),
        )
        return cur.fetchone()


def _claim_next_due() -> tuple | None:
    """Atomically claim the next due row: a due 'pending' row, or a 'sending' row
    whose lease has expired (crashed worker). FOR UPDATE SKIP LOCKED guarantees
    concurrent sweepers/workers never grab the same row."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""update email_outbox set status = %s, locked_at = now(), attempts = attempts + 1
                where id = (
                    select id from email_outbox
                    where (status = %s and next_attempt_at <= now())
                       or (status = %s and locked_at < now() - make_interval(secs => %s))
                    order by next_attempt_at
                    for update skip locked
                    limit 1
                )
                returning {_COLS}""",
            (SENDING, PENDING, SENDING, settings.email_lease_seconds),
        )
        return cur.fetchone()


def _finish(row_id: int, ok: bool, attempts: int, max_attempts: int, err: str | None) -> None:
    """Record the outcome of a claimed send: sent, dead-lettered, or re-scheduled."""
    with pool().connection() as conn, conn.cursor() as cur:
        if ok:
            cur.execute("update email_outbox set status = %s, sent_at = now(), locked_at = null "
                        "where id = %s", (SENT, row_id))
        elif attempts >= max_attempts:
            cur.execute("update email_outbox set status = %s, failed_at = now(), last_error = %s, "
                        "locked_at = null where id = %s", (FAILED, err, row_id))
        else:
            # FIXED interval (not exponential) — retry_interval seconds from now.
            cur.execute("update email_outbox set status = %s, "
                        "next_attempt_at = now() + make_interval(secs => %s), "
                        "last_error = %s, locked_at = null where id = %s",
                        (PENDING, settings.email_retry_interval_seconds, err, row_id))


def _send_claimed(row: tuple) -> bool:
    row_id, to, subject, body, html, attempts, max_attempts = row
    ok = send_email(to, subject, body, html=html)
    _finish(row_id, ok, attempts, max_attempts, None if ok else "send failed")
    return ok


def _try_send_now(row_id: int) -> None:
    """Immediate happy-path attempt for a freshly enqueued row."""
    try:
        row = _claim_specific(row_id)
        if row:
            _send_claimed(row)
    except Exception:  # noqa: BLE001 — background thread must never crash the app
        log.exception("immediate send failed for outbox row %s", row_id)


def sweep_once() -> int:
    """Claim and send every currently-due row. Returns how many were attempted."""
    n = 0
    while True:
        row = _claim_next_due()
        if not row:
            return n
        _send_claimed(row)
        n += 1


def reclaim_stuck() -> int:
    """Startup recovery: reset any 'sending' rows (their worker died) to 'pending'.
    Safe at startup because no worker is mid-send yet. Returns rows reset."""
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("update email_outbox set status = %s, locked_at = null where status = %s",
                        (PENDING, SENDING))
            n = max(cur.rowcount, 0)
        if n:
            log.info("email outbox: reclaimed %d stuck 'sending' row(s) at startup", n)
        return n
    except Exception:  # noqa: BLE001
        log.exception("email outbox: startup reclaim failed")
        return 0


_started = False
_start_lock = threading.Lock()


def start_sweeper() -> None:
    """Launch the background retry sweeper once (idempotent)."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    def _loop() -> None:
        while True:
            try:
                sweep_once()
            except Exception:  # noqa: BLE001 — one bad cycle must not kill the loop
                log.exception("email outbox sweeper cycle failed")
            time.sleep(settings.email_sweep_interval_seconds)

    threading.Thread(target=_loop, daemon=True, name="email-outbox-sweeper").start()
    log.info("email outbox sweeper started (interval=%ss, retry=%ss, max_attempts=%d)",
             settings.email_sweep_interval_seconds, settings.email_retry_interval_seconds,
             settings.email_max_attempts)

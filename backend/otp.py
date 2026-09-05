"""Emailed 6-digit OTP codes: issue, verify, and the login step-up rule.

Codes are stored only as argon2 hashes, expire after OTP_TTL_MINUTES, and are
capped at OTP_MAX_ATTEMPTS wrong guesses (then invalidated) so the small 6-digit
space can't be brute-forced. Issuing a new code of a given kind supersedes any
outstanding one for that (user, kind).

Kinds: 'signup' (verify email at signup), 'reset' (password reset),
'login' (periodic step-up — see login_needs_otp).
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .config import settings
from .db import pool

_ph = PasswordHasher()

SIGNUP, LOGIN, RESET = "signup", "login", "reset"


def generate_code() -> str:
    """A uniformly random 6-digit code, zero-padded (000000–999999)."""
    return f"{secrets.randbelow(1_000_000):06d}"


def issue(user_id: int, kind: str) -> str:
    """Create a fresh code for (user_id, kind) and return the plaintext to email.
    Supersedes any earlier outstanding code of the same kind."""
    code = generate_code()
    code_hash = _ph.hash(code)
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update otp_code set consumed_at = now() "
            "where user_id = %s and kind = %s and consumed_at is null",
            (user_id, kind),
        )
        cur.execute(
            "insert into otp_code (user_id, kind, code_hash, expires_at, max_attempts) "
            "values (%s, %s, %s, now() + make_interval(mins => %s), %s)",
            (user_id, kind, code_hash, settings.otp_ttl_minutes, settings.otp_max_attempts),
        )
        conn.commit()
    return code


def verify(user_id: int, kind: str, code: str) -> bool:
    """Check a submitted code against the latest outstanding one for (user_id, kind).

    On success the code is consumed. On a wrong guess `attempts` is incremented and
    the code is invalidated once the cap is hit. Expired/exhausted codes fail and
    are consumed. Constant-ish: argon2 verify runs whenever a live code exists.
    """
    if not code or not code.isdigit():
        return False
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "select id, code_hash, expires_at, attempts, max_attempts from otp_code "
            "where user_id = %s and kind = %s and consumed_at is null "
            "order by id desc limit 1",
            (user_id, kind),
        )
        row = cur.fetchone()
        if not row:
            return False
        oid, code_hash, expires_at, attempts, max_attempts = row
        if expires_at < datetime.now(timezone.utc) or attempts >= max_attempts:
            cur.execute("update otp_code set consumed_at = now() where id = %s", (oid,))
            conn.commit()
            return False
        try:
            _ph.verify(code_hash, code)
            ok = True
        except VerifyMismatchError:
            ok = False
        if ok:
            cur.execute("update otp_code set consumed_at = now() where id = %s", (oid,))
        else:
            cur.execute("update otp_code set attempts = attempts + 1 where id = %s", (oid,))
            # invalidate immediately once the cap is reached
            cur.execute("update otp_code set consumed_at = now() "
                        "where id = %s and attempts >= max_attempts", (oid,))
        conn.commit()
        return ok


def login_needs_otp(user_id: int) -> bool:
    """Increment the user's login counter and return whether THIS login needs a
    step-up code — every OTP_LOGIN_EVERY-th login (e.g. the 10th, 20th, …)."""
    every = max(settings.otp_login_every, 1)
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update app_user set login_count = login_count + 1 where id = %s "
                    "returning login_count", (user_id,))
        count = cur.fetchone()[0]
        conn.commit()
    return count % every == 0

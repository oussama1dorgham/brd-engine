"""Session-based authentication: argon2id passwords + server-side sessions.

A login/signup mints a random session id stored in `user_session` and set as an
HttpOnly cookie. `require_user` resolves the cookie to a user for protected routes;
deleting the session row (logout) invalidates it everywhere.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request, Response

from .db import pool

_ph = PasswordHasher()
SESSION_COOKIE = "brd_session"
SESSION_TTL_DAYS = 14
# Set BRD_SECURE_COOKIES=1 in production (HTTPS) so the cookie is Secure.
SECURE_COOKIES = os.getenv("BRD_SECURE_COOKIES", "0").strip().lower() in ("1", "true", "yes")


class EmailTaken(Exception):
    """Raised when signing up with an already-registered email."""


def create_user(email: str, password: str) -> dict:
    email = email.strip().lower()
    pw_hash = _ph.hash(password)
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "insert into app_user (email, password_hash) values (%s, %s) returning id, email",
                (email, pw_hash),
            )
            row = cur.fetchone()
            conn.commit()
    except psycopg.errors.UniqueViolation:
        raise EmailTaken(email)
    return {"id": row[0], "email": row[1], "email_verified": False}


def authenticate(email: str, password: str) -> dict | None:
    email = email.strip().lower()
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select id, email, password_hash, email_verified from app_user where email = %s", (email,))
        row = cur.fetchone()
    if not row:
        return None
    try:
        _ph.verify(row[2], password)
    except VerifyMismatchError:
        return None
    return {"id": row[0], "email": row[1], "email_verified": row[3]}


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    exp = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into user_session (id, user_id, expires_at) values (%s, %s, %s)",
            (token, user_id, exp),
        )
        conn.commit()
    return token


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "select u.id, u.email, u.email_verified, s.expires_at from user_session s "
            "join app_user u on u.id = s.user_id where s.id = %s",
            (token,),
        )
        row = cur.fetchone()
        if not row:
            return None
        if row[3] < datetime.now(timezone.utc):
            cur.execute("delete from user_session where id = %s", (token,))
            conn.commit()
            return None
        cur.execute("update user_session set last_seen = now() where id = %s", (token,))
        conn.commit()
    return {"id": row[0], "email": row[1], "email_verified": row[2]}


def delete_session(token: str | None) -> None:
    if not token:
        return
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("delete from user_session where id = %s", (token,))
        conn.commit()


def set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_TTL_DAYS * 86400,
        httponly=True, samesite="lax", secure=SECURE_COOKIES, path="/",
    )


def clear_session_cookie(resp: Response) -> None:
    resp.delete_cookie(SESSION_COOKIE, path="/")


def current_user(request: Request) -> dict | None:
    return session_user(request.cookies.get(SESSION_COOKIE))


def require_user(request: Request) -> dict:
    """Human, cookie-session only. Use for endpoints tokens must NEVER reach
    (password/BYOK management, BRD writes). Returns {id, email, email_verified}."""
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="not authenticated")
    return u


# --- dual auth: human cookie OR scoped service token ------------------------

def _bearer(request: Request) -> str | None:
    h = request.headers.get("authorization") or ""
    return h[7:].strip() if h[:7].lower() == "bearer " else None


def require_scope(scope: str):
    """Dependency factory: allow a human session (full access) OR a service token
    that carries `scope`. Enforces the per-token rate limit and stashes the resolved
    Principal on request.state for the audit middleware. Returns a user-like dict
    with `id` = owner_id (so downstream per-owner queries are unchanged) and `kind`."""
    from . import service_auth

    def dep(request: Request) -> dict:
        u = current_user(request)
        if u:                                   # human: first-party, satisfies any scope
            return {**u, "kind": "human"}
        raw = _bearer(request)
        if raw:
            p = service_auth.resolve_token(raw)
            if p is not None:
                request.state.principal = p   # stash first so denials are audited too
                if not service_auth.has_scope(p, scope):
                    raise HTTPException(status_code=403, detail=f"token missing required scope: {scope}")
                retry = service_auth.check_rate(p)
                if retry is not None:
                    raise HTTPException(status_code=429, detail="rate limit exceeded",
                                        headers={"Retry-After": str(int(retry))})
                return {"id": p.owner_id, "kind": "service", "principal": p}
        raise HTTPException(status_code=401, detail="not authenticated")

    dep.token_scope = scope   # discoverable by route introspection (GET /token-endpoints)
    return dep


def enforce_project(request: Request, user: dict, project: str | None) -> None:
    """Project fence for service tokens: 403 unless the token's account is granted
    `project`. No-op for humans (their queries are already owner-scoped). Records the
    project for the audit trail."""
    if user.get("kind") == "service":
        from . import service_auth
        if not service_auth.can_access_project(user["principal"], project):
            raise HTTPException(status_code=403, detail="token not granted access to this project")
    request.state.audit_project = project


# --- one-time tokens (email verification + password reset) -----------------
VERIFY_TTL_HOURS = 48
RESET_TTL_HOURS = 1


def create_token(user_id: int, kind: str, ttl_hours: int) -> str:
    token = secrets.token_urlsafe(32)
    exp = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into auth_token (token, user_id, kind, expires_at) values (%s, %s, %s, %s)",
            (token, user_id, kind, exp),
        )
        conn.commit()
    return token


def consume_token(token: str, kind: str) -> int | None:
    """Return the user_id for a valid, unused, unexpired token and mark it used."""
    if not token:
        return None
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select user_id, expires_at, used_at from auth_token where token = %s and kind = %s", (token, kind))
        row = cur.fetchone()
        if not row or row[2] is not None or row[1] < datetime.now(timezone.utc):
            return None
        cur.execute("update auth_token set used_at = now() where token = %s", (token,))
        conn.commit()
        return row[0]


def mark_verified(user_id: int) -> None:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update app_user set email_verified = true where id = %s", (user_id,))
        conn.commit()


def set_password(user_id: int, password: str) -> None:
    pw_hash = _ph.hash(password)
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update app_user set password_hash = %s where id = %s", (pw_hash, user_id))
        conn.commit()


def delete_user_sessions(user_id: int) -> None:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("delete from user_session where user_id = %s", (user_id,))
        conn.commit()


def user_by_email(email: str) -> dict | None:
    email = email.strip().lower()
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select id, email from app_user where email = %s", (email,))
        row = cur.fetchone()
    return {"id": row[0], "email": row[1]} if row else None


# --- in-memory rate limiter (per-key sliding window) -----------------------
_rl_lock = threading.Lock()
_rl: dict[str, list[float]] = {}


def rate_limit(key: str, max_hits: int, window_sec: float) -> float | None:
    """Record a hit; return retry-after seconds if over the limit, else None.

    Per-process — fine for a single uvicorn worker; use a shared store (Redis)
    if you scale to multiple workers.
    """
    now = time.monotonic()
    with _rl_lock:
        hits = [t for t in _rl.get(key, []) if now - t < window_sec]
        if len(hits) >= max_hits:
            _rl[key] = hits
            return max(window_sec - (now - hits[0]), 1.0)
        hits.append(now)
        _rl[key] = hits
        return None

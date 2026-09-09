"""Service accounts + scoped API tokens for external (machine) access.

A service account is a non-human identity linked to a human (owner). It is granted
a subset of that owner's BRDs (projects) and, per token, a set of scopes. External
callers authenticate with `Authorization: Bearer <token>`; requests then act AS the
owner (reusing the existing per-owner isolation) but are additionally fenced by:

  * scope     — the token must carry the endpoint's required scope (least privilege)
  * project   — a named `project` must be in the account's grants
  * rate      — a per-token per-minute limit

Only a SHA-256 hash of each token is stored (deterministic → indexable; tokens are
high-entropy so a fast hash is appropriate, unlike passwords). The raw token is
returned exactly once, at issue. This module is credential resolution + management;
the HTTP dependency and endpoints that use it come in later phases.

    python -m backend.service_auth        # tiny self-check (no DB)
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime

from psycopg.types.json import Jsonb

from .db import pool

# Scopes, least-privilege. External consumers get {ask, read}; write/admin are for
# first-party use only and are never granted to third-party tokens by policy.
SCOPES = frozenset({"ask", "read", "write", "admin"})
DEFAULT_SCOPES = ("ask", "read")

TOKEN_PREFIX = "brdsk_"          # BRD service key — identifies our tokens on sight
DEFAULT_RATE_PER_MIN = 60


@dataclass(frozen=True)
class Principal:
    """The resolved identity of a token-authenticated request. `owner_id` is the
    human the service account acts on behalf of, so downstream per-owner queries are
    unchanged; `scopes`/`projects` are the extra fences the caller must satisfy."""
    owner_id: int
    token_id: int
    service_account_id: int
    scopes: frozenset[str]
    projects: frozenset[str]
    rate_limit_per_min: int
    kind: str = "service"


# --- hashing / token generation --------------------------------------------

def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generate() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)   # ~256 bits of entropy


def _mask(raw: str) -> str:
    return f"{TOKEN_PREFIX}…{raw[-4:]}"


def _clean_scopes(scopes) -> list[str]:
    """Keep only known scopes, deduped, order-stable."""
    seen: set[str] = set()
    out: list[str] = []
    for s in scopes or ():
        s = (s or "").strip()
        if s in SCOPES and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# --- resolution (the security-critical path) -------------------------------

def resolve_token(raw: str) -> Principal | None:
    """Resolve a bearer token to a Principal, or None if it is missing / malformed /
    unknown / revoked / expired, or its account is disabled. Fail-CLOSED: any error
    denies (returns None) rather than raising into the request."""
    if not raw or not raw.startswith(TOKEN_PREFIX):
        return None
    token_hash = _hash(raw)
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """select t.id, t.service_account_id, t.scopes, t.rate_limit_per_min,
                          sa.owner_id
                     from api_token t
                     join service_account sa on sa.id = t.service_account_id
                    where t.token_hash = %s
                      and t.revoked_at is null
                      and (t.expires_at is null or t.expires_at > now())
                      and sa.disabled = false""",
                (token_hash,),
            )
            row = cur.fetchone()
            if not row:
                return None
            token_id, sa_id, scopes, rate, owner_id = row
            cur.execute("select project from service_account_project where service_account_id = %s", (sa_id,))
            projects = frozenset(r[0] for r in cur.fetchall())
            cur.execute("update api_token set last_used_at = now() where id = %s", (token_id,))
            conn.commit()
    except Exception:  # noqa: BLE001 — auth must fail closed, never 500 on a DB blip
        return None
    scope_set = frozenset(s for s in scopes if isinstance(s, str)) if isinstance(scopes, list) else frozenset()
    return Principal(owner_id=owner_id, token_id=token_id, service_account_id=sa_id,
                     scopes=scope_set, projects=projects, rate_limit_per_min=int(rate))


def has_scope(principal: Principal, scope: str) -> bool:
    return scope in principal.scopes


def can_access_project(principal: Principal, project: str | None) -> bool:
    """True if the account may act on `project`. A request that names no project
    (project None/"") is allowed here — project-scoped endpoints pass the real one."""
    if not project:
        return True
    return project in principal.projects


def check_rate(principal: Principal) -> float | None:
    """Per-token sliding-window limit; returns retry-after seconds if over, else None."""
    from .auth import rate_limit
    return rate_limit(f"apitoken:{principal.token_id}", principal.rate_limit_per_min, 60.0)


def record_audit(token_id: int | None, endpoint: str, project: str | None,
                 status: int, ip: str | None = None) -> None:
    """Write one audit row. Never raises into a request."""
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "insert into api_audit (token_id, endpoint, project, status, ip) "
                "values (%s, %s, %s, %s, %s)",
                (token_id, endpoint, project, status, ip),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        pass


# --- management (owner-scoped; drives the settings UI in a later phase) ------

def create_service_account(owner_id: int, name: str) -> dict:
    name = (name or "").strip() or "service account"
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into service_account (owner_id, name) values (%s, %s) returning id, created_at",
            (owner_id, name),
        )
        sid, created = cur.fetchone()
        conn.commit()
    return {"id": sid, "name": name, "disabled": False, "created_at": created}


def _owns_account(cur, owner_id: int, sa_id: int) -> bool:
    cur.execute("select 1 from service_account where id = %s and owner_id = %s", (sa_id, owner_id))
    return cur.fetchone() is not None


def list_service_accounts(owner_id: int) -> list[dict]:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "select id, name, disabled, created_at from service_account "
            "where owner_id = %s order by id",
            (owner_id,),
        )
        rows = cur.fetchall()
    return [{"id": r[0], "name": r[1], "disabled": r[2], "created_at": r[3]} for r in rows]


def set_account_disabled(owner_id: int, sa_id: int, disabled: bool) -> bool:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("update service_account set disabled = %s where id = %s and owner_id = %s",
                    (disabled, sa_id, owner_id))
        ok = cur.rowcount > 0
        conn.commit()
    return ok


def grant_project(owner_id: int, sa_id: int, project: str) -> bool:
    project = (project or "").strip()
    if not project:
        return False
    with pool().connection() as conn, conn.cursor() as cur:
        if not _owns_account(cur, owner_id, sa_id):
            return False
        cur.execute(
            "insert into service_account_project (service_account_id, project) values (%s, %s) "
            "on conflict do nothing",
            (sa_id, project),
        )
        conn.commit()
    return True


def revoke_project(owner_id: int, sa_id: int, project: str) -> bool:
    with pool().connection() as conn, conn.cursor() as cur:
        if not _owns_account(cur, owner_id, sa_id):
            return False
        cur.execute("delete from service_account_project where service_account_id = %s and project = %s",
                    (sa_id, project))
        conn.commit()
    return True


def list_grants(owner_id: int, sa_id: int) -> list[str]:
    with pool().connection() as conn, conn.cursor() as cur:
        if not _owns_account(cur, owner_id, sa_id):
            return []
        cur.execute("select project from service_account_project where service_account_id = %s order by project", (sa_id,))
        return [r[0] for r in cur.fetchall()]


def issue_token(owner_id: int, sa_id: int, scopes: list[str] | None = None,
                rate_limit_per_min: int = DEFAULT_RATE_PER_MIN,
                expires_at: datetime | None = None) -> dict | None:
    """Mint a token for a service account the caller owns. Returns metadata plus the
    RAW token under 'token' (shown once — never retrievable again), or None if the
    account isn't the owner's."""
    clean = _clean_scopes(scopes if scopes is not None else DEFAULT_SCOPES)
    raw = _generate()
    with pool().connection() as conn, conn.cursor() as cur:
        if not _owns_account(cur, owner_id, sa_id):
            return None
        cur.execute(
            "insert into api_token (service_account_id, token_hash, masked, scopes, "
            "rate_limit_per_min, expires_at) values (%s, %s, %s, %s, %s, %s) "
            "returning id, created_at",
            (sa_id, _hash(raw), _mask(raw), Jsonb(clean), int(rate_limit_per_min), expires_at),
        )
        tid, created = cur.fetchone()
        conn.commit()
    return {"id": tid, "service_account_id": sa_id, "masked": _mask(raw), "scopes": clean,
            "rate_limit_per_min": int(rate_limit_per_min), "expires_at": expires_at,
            "created_at": created, "token": raw}


def list_tokens(owner_id: int, sa_id: int) -> list[dict]:
    with pool().connection() as conn, conn.cursor() as cur:
        if not _owns_account(cur, owner_id, sa_id):
            return []
        cur.execute(
            "select id, masked, scopes, rate_limit_per_min, expires_at, revoked_at, "
            "last_used_at, created_at from api_token where service_account_id = %s order by id",
            (sa_id,),
        )
        rows = cur.fetchall()
    return [{"id": r[0], "masked": r[1], "scopes": r[2], "rate_limit_per_min": r[3],
             "expires_at": r[4], "revoked_at": r[5], "last_used_at": r[6], "created_at": r[7]}
            for r in rows]


def revoke_token(owner_id: int, token_id: int) -> bool:
    """Revoke a token (immediate). Ownership enforced via the account join."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update api_token set revoked_at = now() where id = %s and revoked_at is null "
            "and service_account_id in (select id from service_account where owner_id = %s)",
            (token_id, owner_id),
        )
        ok = cur.rowcount > 0
        conn.commit()
    return ok


if __name__ == "__main__":  # tiny self-check (no DB)
    t = _generate()
    assert t.startswith(TOKEN_PREFIX) and len(_hash(t)) == 64
    assert _clean_scopes(["ask", "ask", "bogus", "read"]) == ["ask", "read"]
    assert _mask(t).startswith(TOKEN_PREFIX) and _mask(t).endswith(t[-4:])
    print("service_auth self-check OK:", _mask(t), "scopes:", sorted(SCOPES))

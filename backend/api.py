"""FastAPI application for the BRD Retrieval Engine (session-authenticated).

Serves the built React SPA and a JSON API. All BRD/chat endpoints require a
logged-in user (session cookie); each user sees only their own BRDs and chats.

Run (dev):   uvicorn backend.api:app --reload --port 8000
Run (prod):  python -m backend.api        (reads BRD_HOST / BRD_PORT)
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote

import psycopg
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import (
    FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.accounts.auth import (
    EmailTaken,
    RESET_TTL_HOURS,
    VERIFY_TTL_HOURS,
    SESSION_COOKIE,
    authenticate,
    clear_session_cookie,
    consume_token,
    create_session,
    create_token,
    create_user,
    current_user,
    delete_session,
    delete_user_sessions,
    enforce_project,
    mark_verified,
    rate_limit,
    require_scope,
    require_user,
    set_password,
    set_session_cookie,
    user_by_email,
)
from datetime import datetime, timedelta, timezone

from backend.core import crypto
from backend.accounts import email_outbox, llm_keys, otp, service_auth
from backend.requirements import versioning
from backend.core.config import settings
from backend.accounts.email_send import app_base_url, build_code, build_reset, build_verification
from backend.chat.history_store import (
    conversation_user_id,
    create_conversation,
    delete_conversation,
    get_conversation,
    get_messages,
    list_conversations,
    list_projects,
    rename_project,
    set_conversation_model,
    set_conversation_project,
)
from backend.chat.session import ChatSession, GenerationCanceled
from backend.requirements.edit import (
    ChunkNotFound,
    StaleRequirement,
    add_requirement,
    list_change_requests,
    list_requirements,
    remove_requirement,
    requirement_history,
    revert_requirement,
    split_requirement,
    update_requirement,
)
from backend.requirements import structure as req_structure
from backend.use_cases import versioning as ucv
from backend.use_cases import audit as ucaudit
from backend.use_cases import testing as uct
from backend.use_cases import suites as ucsuites
from backend.ingest import jobs as ingest_jobs
from backend.ingest import revise as brd_revise
from backend.requirements import change_agent, req_titles
from backend.use_cases import generation as use_cases
from backend.core.voyage import VoyageUnavailable
from backend.llm.providers import registry as provider_registry
from backend.llm import usage as llm_usage
from backend.llm.providers.base import ProviderError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("brd.api")

MAX_QUESTION_CHARS = 2000
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

app = FastAPI(title="BRD Retrieval Engine")


def _busy_error(e: psycopg.OperationalError) -> JSONResponse:
    """A deadlock / lock timeout is a transient conflict (409, retry shortly); a resource /
    program-limit / system-level failure (SQLSTATE class 53/54/58 — out of memory, too many
    connections, disk full, etc.) is a real 500, not something retrying fixes; any other
    OperationalError (lost connection, etc.) is a 503. Never a bare Starlette 500 either way."""
    log.warning("Database operational error", exc_info=True)
    sqlstate = (getattr(e, "sqlstate", None) or "")[:2]
    if sqlstate in ("53", "54", "58"):
        return JSONResponse({"error": "Something went wrong — please try again."}, status_code=500)
    code = 409 if isinstance(e, (psycopg.errors.DeadlockDetected, psycopg.errors.LockNotAvailable)) else 503
    return JSONResponse({"error": "The BRD is busy — try again in a moment."}, status_code=code)


_HEADER_MAX = 200


def _sanitize_header(raw: str) -> str:
    """Strip control characters (incl. NUL, which Postgres' text type rejects outright) and
    bidi override/isolate characters (U+202A-E, U+2066-9 — can visually disguise a filename's
    real extension), trim, and cap length — applied to user-supplied upload headers before
    they reach the DB. Truncates the STEM, not the tail, so a filename's extension survives
    (the upload sniff needs it to recognize .pdf/.docx)."""
    cleaned = re.sub(r"[\x00-\x1f\x7f‪-‮⁦-⁩]", "", raw or "").strip()
    if len(cleaned) <= _HEADER_MAX:
        return cleaned
    dot = cleaned.rfind(".")
    ext = cleaned[dot:] if 0 < dot and len(cleaned) - dot <= 10 else ""
    return cleaned[:_HEADER_MAX - len(ext)] + ext


_storage_health = {"ok": True}
_STORAGE_HEALTH_INTERVAL = 30.0


def _refresh_storage_health() -> None:
    """Best-effort object-store reachability probe. Runs only on the background thread below —
    the S3 client's retries can take ~17s against a dead endpoint, far past any HTTP
    healthcheck timeout, so this must never run on a request thread."""
    try:
        from backend.core import storage
        storage.store().exists("__health_check__")
        _storage_health["ok"] = True
    except Exception:  # noqa: BLE001
        _storage_health["ok"] = False


def _storage_health_loop() -> None:
    while True:
        _refresh_storage_health()
        time.sleep(_STORAGE_HEALTH_INTERVAL)


def _storage_ok() -> bool:
    """Cached object-store reachability flag for /health — refreshed every ~30s by a
    background daemon thread (see `_storage_health_loop`). Never blocks, never raises."""
    return _storage_health["ok"]


@app.middleware("http")
async def _audit_service_tokens(request: Request, call_next):
    """Record one audit row per token-authenticated request (require_scope stashes the
    Principal on request.state). Off the event loop; never affects the response."""
    response = await call_next(request)
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        from starlette.concurrency import run_in_threadpool
        ip = request.client.host if request.client else None
        await run_in_threadpool(
            service_auth.record_audit, principal.token_id, request.url.path,
            getattr(request.state, "audit_project", None), response.status_code, ip,
        )
    return response


if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")


def _fail_stuck_processing() -> None:
    """A BRD left 'processing' with NO durable ingest job behind it (e.g. from before ingestion
    v2) has no worker that will ever finish it — mark it 'failed' so the user can re-upload.
    BRDs with a queued/running job are left alone: the ingest worker resumes them."""
    try:
        from backend.core.db import pool
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("""update brd_document d set status = 'failed',
                                  last_error = coalesce(last_error, 'Processing was interrupted - upload the file again')
                           where d.status = 'processing' and not exists (
                             select 1 from ingest_job j where j.document_id = d.id and j.status in ('queued', 'running'))""")
            if cur.rowcount:
                log.info("startup: marked %d orphaned 'processing' BRD(s) as failed", cur.rowcount)
            conn.commit()
    except Exception:  # noqa: BLE001
        log.exception("startup: failed to reset stuck processing BRDs")


_fail_stuck_processing()
ingest_jobs.start_worker()     # durable BRD ingestion (resumes queued / interrupted jobs)
try:
    ingest_jobs.start_source_gc()   # background sweep of orphaned source objects/vectors
except Exception:  # noqa: BLE001 — a storage outage must never block startup
    log.exception("startup: failed to start source GC")
uct.start_blob_maintenance()   # test evidence → object storage (moves old DB blobs, sweeps orphans)
email_outbox.reclaim_stuck()   # re-arm any email left 'sending' by a dead worker
email_outbox.start_sweeper()   # background fixed-interval retry of failed sends
threading.Thread(target=_storage_health_loop, daemon=True, name="storage-health").start()

# _lock guards the _live registry below. There is no shared ChatSession cache:
# each turn builds a FRESH session (history reloaded from the DB), so a just-
# canceled worker still winding down in the background can never share mutable
# state with the new turn that replaced it.
_lock = threading.Lock()


# --- live generation registry --------------------------------------------------
# Generation runs in a background thread and its answer is persisted at the end,
# so a client that refreshes or leaves mid-answer would otherwise have no way to
# see it's still working. We buffer the tokens per conversation and fan them out
# to any number of subscribers, so a reopened chat can RE-ATTACH: replay what's
# been produced so far, then stream the rest live.
_STREAM_DONE = object()


class _LiveGen:
    """One in-flight answer: a token buffer + fan-out to attached clients."""

    def __init__(self, question: str):
        self.question = question
        self.lock = threading.Lock()
        self.tokens: list[str] = []
        self.subscribers: list[queue.Queue] = []
        self.status = "running"           # running | done | error | canceled
        self.result: dict | None = None   # the final {"done": ...} payload
        self.error: str | None = None
        self.canceled = False             # set by cancel(); the worker bails and skips persistence

    def emit(self, tok: str) -> None:
        with self.lock:
            self.tokens.append(tok)
            for sub in self.subscribers:
                sub.put(("t", tok))

    def cancel(self) -> None:
        # Just flag it; the worker polls this and raises GenerationCanceled, then
        # finish("canceled") notifies subscribers — so there's one signal path.
        with self.lock:
            self.canceled = True

    def finish(self, status: str, result: dict | None = None, error: str | None = None) -> None:
        with self.lock:
            self.status, self.result, self.error = status, result, error
            for sub in self.subscribers:
                if status == "done":
                    sub.put(("done", result))
                elif status == "canceled":
                    sub.put(("canceled", None))
                else:
                    sub.put(("error", error))
                sub.put((_STREAM_DONE, None))
            self.subscribers.clear()

    def subscribe(self):
        """Atomically snapshot buffered tokens and register a live queue.

        Returns (buffered, queue_or_None, status, result, error). A queue is
        handed back only while still running; a finished gen is replayed from the
        snapshot alone (its final payload comes back in result/error).
        """
        with self.lock:
            if self.status != "running":
                return list(self.tokens), None, self.status, self.result, self.error
            sub: queue.Queue = queue.Queue()
            self.subscribers.append(sub)
            return list(self.tokens), sub, self.status, self.result, self.error

    def unsubscribe(self, sub: queue.Queue) -> None:
        with self.lock:
            if sub in self.subscribers:
                self.subscribers.remove(sub)


_live: dict[int, _LiveGen] = {}   # conversation id -> in-flight generation (guarded by _lock)


def _done_payload(cid: int, res: dict) -> dict:
    return {
        "done": True,
        "conversation_id": cid,
        "answer": res["answer"],
        "standalone": res["standalone"],
        "sources": [
            {"n": s["n"], "chunk_id": s.get("chunk_id"), "req_id": s["req_id"], "section": s["section"],
             "project": s["project"], "snippet": _snippet(s.get("text")),
             "full": _fulltext(s.get("text"))}
            for s in res["sources"]
        ],
    }


def _stream_live(live: _LiveGen):
    """NDJSON of an in-flight (or just-finished) generation: replay then live."""
    buffered, sub, status, result, error = live.subscribe()
    for tok in buffered:
        yield json.dumps({"t": tok}) + "\n"
    if sub is None:                       # already finished — replay the outcome
        if status == "done" and result is not None:
            yield json.dumps(result) + "\n"
        elif status == "canceled":
            yield json.dumps({"canceled": True}) + "\n"
        elif status == "error" and error:
            yield json.dumps({"error": error}) + "\n"
        return
    try:
        while True:
            kind, payload = sub.get()
            if kind is _STREAM_DONE:
                break
            if kind == "t":
                yield json.dumps({"t": payload}) + "\n"
            elif kind == "done":
                yield json.dumps(payload) + "\n"
            elif kind == "canceled":
                yield json.dumps({"canceled": True}) + "\n"
            elif kind == "error":
                yield json.dumps({"error": payload}) + "\n"
    finally:
        live.unsubscribe(sub)


def _session_for(cid: int, user: dict) -> ChatSession:
    # Fresh per turn: history comes from the DB, so concurrent workers never share state.
    conv = get_conversation(cid)
    project = conv["project_scope"] if conv else None
    s = ChatSession(project=project, persist=True, user_id=str(user["id"]), owner_id=user["id"])
    s.conversation_id = cid
    s.history = get_messages(cid)
    return s


def _new_conversation(user: dict, project: str | None = None) -> int:
    return create_conversation(user_id=str(user["id"]), project_scope=project)


def _delete(cid: int, user: dict) -> int:
    with _lock:
        _live.pop(cid, None)   # stop tracking any in-flight generation for it
    return delete_conversation(cid, str(user["id"]))


def _owns_conversation(cid: int, user: dict) -> bool:
    return conversation_user_id(cid) == str(user["id"])


def _enforce_conversation_project(request: Request, user: dict, cid: int) -> None:
    """For a service token, the conversation's BRD must be within its grants (a token
    scoped to one project must not read the owner's other-project chats). No-op for humans."""
    if user.get("kind") == "service":
        conv = get_conversation(cid)
        enforce_project(request, user, conv.get("project_scope") if conv else None)


def _snippet(text: str | None, n: int = 44) -> str | None:
    if not text:
        return None
    t = " ".join(text.split())
    return (t[:n].rstrip() + "…") if len(t) > n else t


def _fulltext(text: str | None, n: int = 600) -> str | None:
    if not text:
        return None
    t = " ".join(text.split())
    return (t[:n].rstrip() + "…") if len(t) > n else t


# --- request bodies --------------------------------------------------------
class Credentials(BaseModel):
    email: str = ""
    password: str = ""


class AskBody(BaseModel):
    question: str = ""
    conversation_id: int | None = None
    project: str | None = None
    model: str | None = None   # BYOK: the model picked for this message


class NewBody(BaseModel):
    project: str | None = None


class DeleteBody(BaseModel):
    conversation_id: int | None = None


class DeleteBrdBody(BaseModel):
    project: str = ""


class RenameBrdBody(BaseModel):
    project: str = ""
    title: str = ""


class RequirementUpdateBody(BaseModel):
    chunk_id: int | None = None
    new_text: str = ""


class RequirementAddBody(BaseModel):
    project: str = ""
    text: str = ""
    req_id: str | None = None
    section: str | None = None
    after_chunk_id: int | None = None


class RequirementDeleteBody(BaseModel):
    chunk_id: int | None = None


class RequirementStructureBody(BaseModel):
    chunk_id: int | None = None


class RequirementSplitBody(BaseModel):
    chunk_id: int | None = None
    rows: list[str] = []


class RequirementPlanBody(BaseModel):
    project: str = ""
    story: str = ""
    refine: bool = True
    model: str | None = None


class RequirementApplyBody(BaseModel):
    project: str = ""
    operations: list[dict] = []
    idempotency_key: str | None = None          # dedupes a retried/double-submitted apply
    story: str | None = None                    # provenance: the plain-language request
    refined: str | None = None                  # provenance: the refined instruction it was planned from
    model: str | None = None                    # provenance: the generating model


class RequirementScopeEditBody(BaseModel):
    chunk_id: int | None = None
    change: str = ""
    model: str | None = None


class VersionSnapshotBody(BaseModel):
    project: str = ""
    label: str = ""


class VersionRestoreBody(BaseModel):
    project: str = ""
    snapshot_id: int | None = None


class ProjectActionBody(BaseModel):
    project: str = ""
    label: str = ""


# --- auth ------------------------------------------------------------------
class ForgotBody(BaseModel):
    email: str = ""


class ResetBody(BaseModel):
    email: str = ""
    code: str = ""
    password: str = ""


class OtpVerifyBody(BaseModel):
    email: str = ""
    code: str = ""
    kind: str = "signup"   # 'signup' | 'login'


class OtpResendBody(BaseModel):
    email: str = ""
    kind: str = "signup"


class ChangePasswordBody(BaseModel):
    current_password: str = ""
    new_password: str = ""


class LlmKeyBody(BaseModel):
    provider: str = "openai"
    base_url: str = ""
    api_key: str = ""
    label: str = ""


class LlmKeyIdBody(BaseModel):
    id: int | None = None


class PreferredModelsBody(BaseModel):
    models: list[str] = []
    key_id: int | None = None


# --- service accounts / API tokens (external access) -----------------------
class ServiceAccountBody(BaseModel):
    name: str = ""


class SaDisableBody(BaseModel):
    disabled: bool = True


class SaGrantBody(BaseModel):
    project: str = ""


class SaIssueTokenBody(BaseModel):
    scopes: list[str] = ["ask", "read"]
    rate_limit_per_min: int = 60
    expires_days: int | None = None


class UseCaseGenerateBody(BaseModel):
    project: str = ""
    replace: bool = False
    mode: str | None = None      # "resume" (default) | "sync" | "replace"; falls back to `replace`
    model: str | None = None
    coverage: str | None = None  # "functional" (default) | "full" (adds negative + edge cases)


class UseCaseRestoreBody(BaseModel):
    uid: int | None = None
    version_no: int | None = None


class UseCaseUpdateBody(BaseModel):
    id: int | None = None
    title: str | None = None
    description: str | None = None
    roles: list[str] | None = None
    preconditions: str | None = None
    steps: list[str] | None = None
    expected_behaviour: str | None = None
    case_type: str | None = None   # functional | negative | edge (validated server-side)


class UseCaseIdBody(BaseModel):
    id: int | None = None


class UseCaseMoveBody(BaseModel):
    id: int | None = None
    folder_id: int | None = None


class UseCaseTestStatusBody(BaseModel):
    id: int | None = None
    status: str | None = None
    note: str | None = None
    event_kind: str | None = None   # "status" (default) | "note" (a log entry, no verdict change)
    defect_ref: str | None = None   # failed/blocked: a bug-tracker link or id
    cycle_id: int | None = None     # record into this test cycle (None = ad-hoc)


class UseCaseTestBulkBody(BaseModel):
    ids: list[int] = []
    status: str | None = None
    note: str | None = None
    cycle_id: int | None = None


class UseCaseTestUndoBody(BaseModel):
    result_id: int | None = None
    cycle_id: int | None = None


class UseCaseTestEditBody(BaseModel):
    result_id: int | None = None
    note: str | None = None
    defect_ref: str | None = None


class UseCaseProofLinkBody(BaseModel):
    id: int | None = None
    url: str = ""
    caption: str | None = None


class UseCaseProofIdBody(BaseModel):
    proof_id: int | None = None
    caption: str | None = None


class UseCaseScopeBody(BaseModel):
    id: int | None = None
    in_scope: bool = True        # false = mark out of scope (excluded from coverage)
    note: str | None = None
    cycle_id: int | None = None


class UseCaseScopeBulkBody(BaseModel):
    ids: list[int] = []
    in_scope: bool = True


class TestCycleCreateBody(BaseModel):
    project: str = ""
    name: str = ""
    description: str | None = None
    source: str = "all"                     # all | not_passed | failed_blocked | retest | empty
    case_types: list[str] | None = None
    folder_ids: list[int] | None = None
    from_cycle_id: int | None = None        # narrow to a previous cycle's members / statuses
    carry: bool = True                      # carry passes of cases unchanged since they passed


class SuitePublishBody(BaseModel):
    project: str | None = None
    label: str | None = None
    expect: str | None = None               # the draft fingerprint the user reviewed (stale guard)


class SuiteRestoreBody(BaseModel):
    project: str | None = None
    version_id: int | None = None


class SuiteLabelBody(BaseModel):
    project: str | None = None
    version_id: int | None = None
    label: str | None = None


class TestCycleUpdateBody(BaseModel):
    cycle_id: int | None = None
    name: str | None = None
    description: str | None = None
    status: str | None = None               # open | closed


class TestCycleMembersBody(BaseModel):
    cycle_id: int | None = None
    add_ids: list[int] | None = None        # use_case row ids to plan in
    remove_uids: list[int] | None = None    # use_case uids to plan out (results are kept)


class UcFolderCreateBody(BaseModel):
    project: str = ""
    parent_id: int | None = None
    name: str = ""


class UcFolderRenameBody(BaseModel):
    folder_id: int | None = None
    name: str = ""


class UcFolderIdBody(BaseModel):
    folder_id: int | None = None


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _valid_credentials(c: Credentials) -> str | None:
    email = c.email.strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        return "Enter a valid email address."
    if len(c.password) < 8:
        return "Password must be at least 8 characters."
    return None


def _user_payload(user: dict) -> dict:
    return {"id": user["id"], "email": user["email"], "email_verified": bool(user.get("email_verified"))}


def _send_code(user_id: int, email: str, kind: str) -> None:
    """Issue a fresh OTP and enqueue it (durable outbox). kind: signup|login|reset."""
    code = otp.issue(user_id, kind)
    subject, plain, html = build_code(kind, code, settings.otp_ttl_minutes)
    email_outbox.enqueue(email, subject, plain, html=html, user_id=user_id)


# LEGACY (kept, currently unused): email-verification via a clickable link. The
# active flow uses 6-digit codes (_send_code); this remains for a future link mode.
def _send_verification(user: dict) -> None:
    token = create_token(user["id"], "verify", VERIFY_TTL_HOURS)
    link = f"{app_base_url()}/auth/verify?token={token}"
    subject, plain, html = build_verification(link, VERIFY_TTL_HOURS)
    email_outbox.enqueue(user["email"], subject, plain, html=html, user_id=user["id"])


@app.post("/auth/signup")
def signup(body: Credentials, request: Request, response: Response):
    # Rate-limit by IP: signup triggers a verification email, so an unthrottled
    # endpoint is an open mail-spam amplifier to arbitrary addresses.
    retry = rate_limit(f"signup:{_client_ip(request)}", max_hits=5, window_sec=3600)
    if retry is not None:
        return JSONResponse({"error": "Too many attempts. Try again shortly."},
                            status_code=429, headers={"Retry-After": str(int(retry))})
    err = _valid_credentials(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    try:
        user = create_user(body.email, body.password)
    except EmailTaken:
        return JSONResponse({"error": "That email is already registered."}, status_code=409)
    # Blocking gate: no session yet — email a 6-digit code and make the client
    # verify it (via /auth/otp/verify) before any session cookie is issued.
    _send_code(user["id"], user["email"], otp.SIGNUP)
    return {"stage": "otp", "kind": "signup", "email": user["email"]}


@app.post("/auth/login")
def login(body: Credentials, request: Request, response: Response):
    retry = rate_limit(f"login:{_client_ip(request)}", max_hits=10, window_sec=300)
    if retry is not None:
        return JSONResponse({"error": "Too many attempts. Try again shortly."},
                            status_code=429, headers={"Retry-After": str(int(retry))})
    user = authenticate(body.email, body.password)
    if not user:
        return JSONResponse({"error": "Wrong email or password."}, status_code=401)
    # Unverified account (e.g. abandoned signup): route back through email verification.
    if not user["email_verified"]:
        _send_code(user["id"], user["email"], otp.SIGNUP)
        return {"stage": "otp", "kind": "signup", "email": user["email"]}
    # Periodic step-up: every OTP_LOGIN_EVERY-th login needs an emailed code.
    if otp.login_needs_otp(user["id"]):
        _send_code(user["id"], user["email"], otp.LOGIN)
        return {"stage": "otp", "kind": "login", "email": user["email"]}
    set_session_cookie(response, create_session(user["id"]))
    return _user_payload(user)


@app.post("/auth/otp/verify")
def otp_verify(body: OtpVerifyBody, request: Request, response: Response):
    retry = rate_limit(f"otp:{_client_ip(request)}", max_hits=10, window_sec=300)
    if retry is not None:
        return JSONResponse({"error": "Too many attempts. Try again shortly."},
                            status_code=429, headers={"Retry-After": str(int(retry))})
    kind = body.kind if body.kind in (otp.SIGNUP, otp.LOGIN) else otp.SIGNUP
    user = user_by_email(body.email)
    if not user or not otp.verify(user["id"], kind, body.code):
        return JSONResponse({"error": "That code is invalid or has expired."}, status_code=400)
    if kind == otp.SIGNUP:
        mark_verified(user["id"])
    set_session_cookie(response, create_session(user["id"]))
    return {"id": user["id"], "email": user["email"], "email_verified": True}


@app.post("/auth/otp/resend")
def otp_resend(body: OtpResendBody, request: Request):
    retry = rate_limit(f"otp-resend:{_client_ip(request)}", max_hits=3, window_sec=300)
    if retry is not None:
        return JSONResponse({"error": "Too many requests. Try again shortly."},
                            status_code=429, headers={"Retry-After": str(int(retry))})
    kind = body.kind if body.kind in (otp.SIGNUP, otp.LOGIN, otp.RESET) else otp.SIGNUP
    user = user_by_email(body.email)
    if user:  # silent when the address is unknown (don't reveal existence)
        _send_code(user["id"], user["email"], kind)
    return {"ok": True}


@app.post("/auth/logout")
def logout(request: Request, response: Response):
    delete_session(request.cookies.get(SESSION_COOKIE))
    clear_session_cookie(response)
    return {"ok": True}


@app.get("/auth/me")
def me(request: Request):
    u = current_user(request)
    if not u:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    return _user_payload(u)


@app.get("/auth/verify")
def verify_email(token: str = ""):
    user_id = consume_token(token, "verify")
    if user_id is not None:
        mark_verified(user_id)
        return RedirectResponse(url="/?verified=1", status_code=303)
    return RedirectResponse(url="/?verified=0", status_code=303)


@app.post("/auth/resend-verification")
def resend_verification(user: dict = Depends(require_user)):
    if user.get("email_verified"):
        return {"ok": True, "already": True}
    # Cap resends per account so a logged-in user can't spam their own inbox.
    retry = rate_limit(f"resend:{user['id']}", max_hits=3, window_sec=3600)
    if retry is not None:
        return JSONResponse({"error": "Too many requests. Try again later."},
                            status_code=429, headers={"Retry-After": str(int(retry))})
    _send_verification(user)
    return {"ok": True}


@app.post("/auth/change-password")
def change_password(body: ChangePasswordBody, user: dict = Depends(require_user)):
    if len(body.new_password) < 8:
        return JSONResponse({"error": "New password must be at least 8 characters."}, status_code=400)
    if not authenticate(user["email"], body.current_password):
        return JSONResponse({"error": "Current password is incorrect."}, status_code=400)
    set_password(user["id"], body.new_password)
    return {"ok": True}


# --- Custom LLM: bring-your-own-key (OpenAI-compatible) --------------------
@app.get("/llm/providers")
def llm_providers(user: dict = Depends(require_user)):
    """Providers the UI can offer + whether BYOK is enabled on this server."""
    return {"enabled": crypto.available(), "providers": provider_registry.providers_meta()}


@app.get("/llm/key")
def llm_get_key(user: dict = Depends(require_user)):
    """Active-key summary — used by the app/chat to know if generation is BYOK."""
    if not crypto.available():
        return {"available": False, "configured": False}
    meta = llm_keys.get_meta(user["id"]) or {"configured": False}
    return {"available": True, **meta}


@app.get("/llm/keys")
def llm_list_keys(user: dict = Depends(require_user)):
    """All of the user's keys (for the settings list)."""
    if not crypto.available():
        return {"available": False, "keys": []}
    return {"available": True, "keys": llm_keys.list_keys(user["id"])}


@app.post("/llm/key")
def llm_add_key(body: LlmKeyBody, user: dict = Depends(require_user)):
    """Add a new key (validated + encrypted) and make it the active one."""
    if not crypto.available():
        return JSONResponse({"error": "Custom keys are not enabled on this server."}, status_code=503)
    provider = (body.provider or "openai").strip()
    if not provider_registry.is_known(provider):
        return JSONResponse({"error": "Unknown provider."}, status_code=400)
    api_key, base_url = body.api_key.strip(), body.base_url.strip()
    if not api_key:
        return JSONResponse({"error": "API key is required."}, status_code=400)
    adapter = provider_registry.get(provider)
    if adapter.needs_base_url:
        if not base_url:
            return JSONResponse({"error": "Base URL is required for this provider."}, status_code=400)
        if not base_url.startswith(("http://", "https://")):
            return JSONResponse({"error": "Base URL must start with http:// or https://"}, status_code=400)
    try:
        models = llm_keys.validate_and_list(provider, base_url, api_key)
    except llm_keys.InvalidKey as e:
        return JSONResponse({"error": f"Could not validate that key: {e}"}, status_code=400)
    meta = llm_keys.add_key(user["id"], provider, base_url, api_key, body.label, models)
    return {"ok": True, **meta, "models": models}


@app.post("/llm/keys/active")
def llm_set_active(body: LlmKeyIdBody, user: dict = Depends(require_user)):
    if body.id is None or not llm_keys.set_active(user["id"], body.id):
        return JSONResponse({"error": "Key not found."}, status_code=404)
    return {"ok": True, "active": body.id}


@app.post("/llm/key/delete")
def llm_delete_key(body: LlmKeyIdBody, user: dict = Depends(require_user)):
    if body.id is None or not llm_keys.delete_key(user["id"], body.id):
        return JSONResponse({"error": "Key not found."}, status_code=404)
    return {"ok": True}


@app.get("/llm/models")
def llm_list_models(user: dict = Depends(require_user)):
    # `models` = everything the key can reach (feeds the "choose your models" modal);
    # `preferred` = the user's curated subset (feeds the in-chat picker; [] = show all).
    return {
        "models": llm_keys.list_models(user["id"]),
        "preferred": llm_keys.get_preferred(user["id"]),
    }


@app.post("/llm/preferred")
def llm_set_preferred(body: PreferredModelsBody, user: dict = Depends(require_user)):
    if not crypto.available():
        return JSONResponse({"error": "Custom keys are not enabled on this server."}, status_code=503)
    if llm_keys.get_meta(user["id"]) is None:
        return JSONResponse({"error": "No API key configured."}, status_code=400)
    return {"ok": True, "preferred": llm_keys.set_preferred(user["id"], body.models, body.key_id)}


# --- service accounts + API tokens (external access; human-only management) --
_TOKEN_SCOPES = ("ask", "read")   # scopes the UI may grant (write/admin never for tokens)


@app.get("/service-accounts")
def sa_list(user: dict = Depends(require_user)):
    accounts = []
    for a in service_auth.list_service_accounts(user["id"]):
        a["grants"] = service_auth.list_grants(user["id"], a["id"])
        a["tokens"] = service_auth.list_tokens(user["id"], a["id"])
        accounts.append(a)
    return {"accounts": accounts}


@app.post("/service-accounts")
def sa_create(body: ServiceAccountBody, user: dict = Depends(require_user)):
    if not (body.name or "").strip():
        return JSONResponse({"error": "name is required"}, status_code=400)
    return service_auth.create_service_account(user["id"], body.name)


@app.post("/service-accounts/{sid}/delete")
def sa_delete(sid: int, user: dict = Depends(require_user)):
    if not service_auth.delete_service_account(user["id"], sid):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


@app.post("/service-accounts/{sid}/disable")
def sa_disable(sid: int, body: SaDisableBody, user: dict = Depends(require_user)):
    if not service_auth.set_account_disabled(user["id"], sid, body.disabled):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True, "disabled": body.disabled}


@app.post("/service-accounts/{sid}/grant")
def sa_grant(sid: int, body: SaGrantBody, user: dict = Depends(require_user)):
    if not service_auth.grant_project(user["id"], sid, body.project):
        return JSONResponse({"error": "not found or empty project"}, status_code=404)
    return {"ok": True, "grants": service_auth.list_grants(user["id"], sid)}


@app.post("/service-accounts/{sid}/revoke-grant")
def sa_revoke_grant(sid: int, body: SaGrantBody, user: dict = Depends(require_user)):
    if not service_auth.revoke_project(user["id"], sid, body.project):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True, "grants": service_auth.list_grants(user["id"], sid)}


@app.post("/service-accounts/{sid}/tokens")
def sa_issue_token(sid: int, body: SaIssueTokenBody, user: dict = Depends(require_user)):
    scopes = [s for s in (body.scopes or []) if s in _TOKEN_SCOPES] or ["ask", "read"]
    expires_at = None
    if body.expires_days and body.expires_days > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_days)
    rate = max(1, min(int(body.rate_limit_per_min or 60), 6000))
    meta = service_auth.issue_token(user["id"], sid, scopes, rate, expires_at)
    if meta is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return meta   # includes the raw 'token' — shown exactly once


@app.post("/tokens/{tid}/revoke")
def sa_revoke_token(tid: int, user: dict = Depends(require_user)):
    if not service_auth.revoke_token(user["id"], tid):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


@app.get("/token-endpoints")
def token_endpoints(user: dict = Depends(require_user)):
    """Token-accessible endpoints, derived from the LIVE routes: every require_scope
    route that carries a `summary`. The API-access UI renders this, so it never drifts
    from the backend — add a require_scope endpoint with a summary and it shows up."""
    from fastapi.routing import APIRoute
    out = []
    for r in app.routes:
        if not isinstance(r, APIRoute) or not r.summary:
            continue
        scope = next((getattr(d.call, "token_scope", None) for d in r.dependant.dependencies
                      if getattr(d.call, "token_scope", None)), None)
        if not scope:
            continue
        method = next((m for m in ("GET", "POST", "PUT", "DELETE") if m in r.methods), "GET")
        out.append({"scope": scope, "method": method, "path": r.path, "title": r.summary})
    out.sort(key=lambda e: (e["scope"] != "ask", e["path"]))
    return {"endpoints": out}


@app.post("/auth/forgot")
def forgot(body: ForgotBody, request: Request):
    retry = rate_limit(f"forgot:{_client_ip(request)}", max_hits=5, window_sec=300)
    if retry is not None:
        return JSONResponse({"error": "Too many attempts. Try again shortly."},
                            status_code=429, headers={"Retry-After": str(int(retry))})
    user = user_by_email(body.email)
    if user:  # email a reset CODE; stay silent about whether the address exists
        _send_code(user["id"], user["email"], otp.RESET)
    # Always advance the client to code entry so existence isn't revealed.
    return {"stage": "otp", "kind": "reset", "email": body.email.strip().lower()}


@app.post("/auth/reset")
def reset(body: ResetBody, response: Response):
    if len(body.password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters."}, status_code=400)
    user = user_by_email(body.email)
    if not user or not otp.verify(user["id"], otp.RESET, body.code):
        return JSONResponse({"error": "That code is invalid or has expired."}, status_code=400)
    user_id = user["id"]
    set_password(user_id, body.password)
    delete_user_sessions(user_id)                 # log out everywhere for safety
    mark_verified(user_id)                         # a successful reset proves email ownership
    set_session_cookie(response, create_session(user_id))
    return {"id": user_id, "email": user["email"], "email_verified": True}


# --- GET endpoints ---------------------------------------------------------
@app.get("/")
def index(request: Request):
    entry = FRONTEND_DIST / "index.html"
    if not entry.exists():
        return PlainTextResponse("Frontend not built. Run: cd frontend && npm run build", status_code=503)
    return FileResponse(entry, media_type="text/html; charset=utf-8")


@app.get("/favicon.svg")
@app.get("/favicon.ico")   # browsers probe /favicon.ico by default; serve the bear for both
def favicon():
    f = FRONTEND_DIST / "favicon.svg"
    if not f.exists():
        return PlainTextResponse("", status_code=404)
    return FileResponse(f, media_type="image/svg+xml")


@app.get("/health")
def health():
    ok = True
    try:
        from backend.core.db import pool
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select 1")
            cur.fetchone()
    except Exception:  # noqa: BLE001
        ok = False
    return JSONResponse({"status": "ok" if ok else "degraded", "db": ok, "storage": _storage_ok()},
                         status_code=200 if ok else 503)


@app.get("/conversations", summary="List past Q&A conversations for granted BRDs")
def conversations(user: dict = Depends(require_scope("read")), limit: int = 30, before: int | None = None):
    """A page of the user's conversations (newest first). `before` = the last id
    you've seen (keyset cursor); `has_more` tells the client to keep lazy-loading."""
    limit = max(1, min(limit, 100))
    rows = list_conversations(str(user["id"]), limit + 1, before)   # +1 to detect more
    has_more = len(rows) > limit
    result = rows[:limit]
    if user.get("kind") == "service":   # a token sees only chats in its granted BRDs
        allowed = user["principal"].projects
        result = [c for c in result if c.get("project") in allowed]
    return {"conversations": result, "has_more": has_more}


@app.get("/conversation/{cid}", summary="Read one conversation's messages")
def conversation(cid: int, request: Request, user: dict = Depends(require_scope("read"))):
    # cid is a path param (FastAPI validates it as int → 422 on garbage).
    if not _owns_conversation(cid, user):
        return JSONResponse({"error": "not found"}, status_code=404)
    _enforce_conversation_project(request, user, cid)
    conv = get_conversation(cid)
    # include the saved BYOK model so the picker restores the model this chat last used
    return {"messages": get_messages(cid), "model": conv.get("model") if conv else None}


class ConvModelBody(BaseModel):
    model: str | None = None


@app.post("/conversation/{cid}/model", summary="Remember the model picked for a conversation")
def conversation_set_model(cid: int, body: ConvModelBody, user: dict = Depends(require_user)):
    """Persist the chat's BYOK model when the user picks it in the picker (without waiting
    for the next /ask), so it survives a reload / reopen."""
    if not _owns_conversation(cid, user):
        return JSONResponse({"error": "not found"}, status_code=404)
    set_conversation_model(cid, body.model)
    return {"ok": True}


@app.get("/conversation/{cid}/stream")
def conversation_stream(cid: int, request: Request, user: dict = Depends(require_scope("ask"))):
    """Re-attach to an answer still being generated for this conversation.

    Streams `{question}` (so the reopened UI can render the pending turn), then
    the tokens produced so far, then the rest live + the final payload. If nothing
    is generating, emits `{"idle": true}` and the client shows persisted messages.
    """
    if not _owns_conversation(cid, user):
        return JSONResponse({"error": "not found"}, status_code=404)
    _enforce_conversation_project(request, user, cid)
    with _lock:
        live = _live.get(cid)
    if live is None or live.status != "running" or live.canceled:
        return StreamingResponse(
            iter([json.dumps({"idle": True}) + "\n"]),
            media_type="application/x-ndjson; charset=utf-8",
        )

    def gen():
        yield json.dumps({"question": live.question}) + "\n"
        yield from _stream_live(live)

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/projects", summary="List the BRDs this token can access")
def projects(user: dict = Depends(require_scope("read"))):
    projs = list_projects(user["id"])
    if user.get("kind") == "service":   # a token sees only its granted BRDs
        allowed = user["principal"].projects
        projs = [p for p in projs if p["project"] in allowed]
    return {"projects": projs}


@app.get("/starters", summary="Get suggested starter questions for a BRD")
def starters(request: Request, project: str = "", user: dict = Depends(require_scope("read"))):
    project = project.strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    enforce_project(request, user, project)
    try:
        from backend.chat.suggest import generate_starters
        return {"questions": generate_starters(project, user["id"], route=_edit_route(user, None))}
    except Exception:  # noqa: BLE001
        log.exception("starters failed")
        return {"questions": []}


# --- POST endpoints --------------------------------------------------------
@app.post("/new")
def new_conversation(request: Request, body: NewBody, user: dict = Depends(require_scope("ask"))):
    if body.project:
        enforce_project(request, user, body.project)
    return {"conversation_id": _new_conversation(user, body.project or None)}


@app.post("/delete")
def delete_conv(body: DeleteBody, user: dict = Depends(require_user)):
    if isinstance(body.conversation_id, int):
        _delete(body.conversation_id, user)
    return {"ok": True}


@app.post("/delete_brd")
def delete_brd(body: DeleteBrdBody, user: dict = Depends(require_user)):
    """Delete a BRD and everything scoped to it in ONE transaction: the BRD's documents,
    its pinned conversations, its use-case tree (folders/cases/version history/test results/
    cycles/proof rows, via use_cases.purge_project_rows — reuses testing.purge_project), and
    the ingest job/source/revision rows. Object-store keys and embedding-cache hashes are
    collected BEFORE anything is deleted; objects/vectors/proof blobs are only removed AFTER
    the transaction commits. A re-upload under the same title then gets a clean slate — no
    orphaned use cases, test results, or requirement basis left behind by the old project.

    If anything in the transaction fails (including a deadlock on a busy BRD), it rolls back
    as a whole: nothing is deleted and the caller gets a clean JSON 500."""
    project = (body.project or "").strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    owner_id = user["id"]
    if _generation_running(user, project):
        return JSONResponse(
            {"error": "Use cases are being generated or updated — delete once that finishes."},
            status_code=409,
        )
    from backend.core.db import pool as _pool
    try:
        with _pool().connection() as conn, conn.cursor() as cur:
            # Collect everything cleanup needs BEFORE any row is removed.
            keys = ingest_jobs.source_keys(cur, owner_id, project)
            hashes = ingest_jobs.brd_embed_hashes(cur, owner_id, project)
            proof_keys = use_cases.purge_project_rows(cur, owner_id, project)
            cur.execute("delete from brd_document where project = %s and owner_id = %s", (project, owner_id))
            removed = cur.rowcount
            cur.execute("delete from conversation where project_scope = %s and user_id = %s",
                       (project, str(owner_id)))
            ingest_jobs.purge_project_rows(cur, owner_id, project)
            # a deleted BRD must stop being reachable through any service-account token too
            cur.execute(
                """delete from service_account_project sap using service_account sa
                   where sap.service_account_id = sa.id and sa.owner_id = %s and sap.project = %s""",
                (owner_id, project),
            )
            conn.commit()
    except psycopg.OperationalError as e:
        return _busy_error(e)
    except Exception:  # noqa: BLE001 — the transaction rolled back; nothing was deleted
        log.exception("delete_brd failed for project=%s owner=%s", project, owner_id)
        return JSONResponse({"error": "Could not delete this BRD — nothing was changed."}, status_code=500)

    # Everything below is best-effort cleanup AFTER the delete committed; none of it can
    # un-delete the BRD, so failures here are logged, not surfaced as an error to the caller.
    try:
        uct._drop_blobs(proof_keys)   # use-case test-proof objects
    except Exception:  # noqa: BLE001
        log.warning("proof blob purge failed for project=%s", project, exc_info=True)
    try:
        purged = ingest_jobs.purge_after_brd_delete(owner_id, keys, hashes)   # source objects + cached vectors
        log.info("delete_brd %s: %s", project, purged)
    except Exception:  # noqa: BLE001 — storage cleanup never blocks the delete (gc catches leftovers)
        log.warning("source/vector purge failed for project=%s", project, exc_info=True)
    from backend.core import cache
    cache.bust_project(project, owner_id=owner_id, namespaces=(cache.RETRIEVE, cache.ANSWER, "reqtitle"))
    try:
        from backend.chat.suggest import invalidate
        invalidate(project, owner_id)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "removed": removed}


@app.post("/rename_brd")
def rename_brd(body: RenameBrdBody, user: dict = Depends(require_user)):
    project = (body.project or "").strip()
    title = _sanitize_header(body.title or "")
    if not project or not title:
        return JSONResponse({"error": "missing project or title"}, status_code=400)
    updated = rename_project(project, title, user["id"])
    return {"ok": True, "title": title, "updated": updated}


# --- Change a requirement (QA): edit a requirement's text, re-embed + re-index ---
def _archived_requirements(owner_id: int, project: str) -> list[dict]:
    """Archived requirements of a BRD (brd_chunk_archive) — same shape as list_requirements,
    plus archived=True and retired_at, so citations of a retired requirement still resolve."""
    from backend.core.db import pool
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """select a.id, a.req_id, a.section, a.ordinal, a.text, a.section_path, a.page, a.retired_at
               from brd_chunk_archive a join brd_document d on d.id = a.document_id
               where d.owner_id = %s and d.project = %s
               order by a.retired_at desc""",
            (owner_id, project),
        )
        return [{"chunk_id": r[0], "req_id": r[1], "section": r[2], "ordinal": r[3], "text": r[4],
                 "section_path": r[5], "page": r[6], "archived": True,
                 "retired_at": r[7].isoformat() if r[7] else None}
                for r in cur.fetchall()]


@app.get("/brd/requirements", summary="Read all requirements of a granted BRD")
def brd_requirements(request: Request, project: str = "", include_archived: int = 0,
                     user: dict = Depends(require_scope("read"))):
    project = (project or "").strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    enforce_project(request, user, project)
    reqs = list_requirements(user["id"], project)
    if include_archived:
        reqs = reqs + _archived_requirements(user["id"], project)
    return {"requirements": reqs}


class ReqTitlesBody(BaseModel):
    project: str = ""
    chunk_ids: list[int] = []


@app.post("/requirements/titles", summary="Short human titles for requirement chunks (citation labels)")
def requirement_titles(body: ReqTitlesBody, request: Request, user: dict = Depends(require_user)):
    """Friendly, cached titles for the given requirement chunks — used as evidence/citation
    labels so tags read as a short phrase instead of a raw chunk. Routes through the user's
    BYOK model when configured; fail-open (missing titles just fall back client-side)."""
    project = (body.project or "").strip()
    if not project or not body.chunk_ids:
        return {"titles": {}}
    enforce_project(request, user, project)
    titles = req_titles.titles_for(user["id"], project, body.chunk_ids[:50], _edit_route(user, None))
    return {"titles": {str(k): v for k, v in titles.items()}}


@app.post("/brd/requirement/update")
def brd_requirement_update(body: RequirementUpdateBody, user: dict = Depends(require_user)):
    if not isinstance(body.chunk_id, int):
        return JSONResponse({"error": "missing chunk_id"}, status_code=400)
    text = (body.new_text or "").strip()
    if not text:
        return JSONResponse({"error": "new text is empty"}, status_code=400)
    if len(text) > 20000:
        return JSONResponse({"error": "requirement text too long (max 20000 chars)"}, status_code=413)
    try:
        res = update_requirement(user["id"], body.chunk_id, text, changed_by=user["id"])
    except ChunkNotFound:
        return JSONResponse({"error": "requirement not found"}, status_code=404)
    except VoyageUnavailable as e:
        return JSONResponse({"error": f"Re-embedding is rate-limited right now: {e}"}, status_code=503)
    except psycopg.OperationalError as e:
        return _busy_error(e)
    except Exception as e:  # noqa: BLE001
        log.exception("requirement update failed")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return {"ok": True, **res}


@app.post("/brd/requirement/add")
def brd_requirement_add(body: RequirementAddBody, user: dict = Depends(require_user)):
    project = (body.project or "").strip()
    text = (body.text or "").strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    if not text:
        return JSONResponse({"error": "text is empty"}, status_code=400)
    if len(text) > 20000:
        return JSONResponse({"error": "requirement text too long (max 20000 chars)"}, status_code=413)
    try:
        res = add_requirement(user["id"], project, text, req_id=(body.req_id or None),
                              section=(body.section or None), after_chunk_id=body.after_chunk_id,
                              changed_by=user["id"])
    except ChunkNotFound:
        return JSONResponse({"error": "BRD or anchor requirement not found"}, status_code=404)
    except VoyageUnavailable as e:
        return JSONResponse({"error": f"Embedding is rate-limited right now: {e}"}, status_code=503)
    except psycopg.OperationalError as e:
        return _busy_error(e)
    except Exception as e:  # noqa: BLE001
        log.exception("requirement add failed")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return {"ok": True, **res}


@app.post("/brd/requirement/delete")
def brd_requirement_delete(body: RequirementDeleteBody, user: dict = Depends(require_user)):
    if not isinstance(body.chunk_id, int):
        return JSONResponse({"error": "missing chunk_id"}, status_code=400)
    try:
        res = remove_requirement(user["id"], body.chunk_id, changed_by=user["id"])
    except ChunkNotFound:
        return JSONResponse({"error": "requirement not found"}, status_code=404)
    except psycopg.OperationalError as e:
        return _busy_error(e)
    return {"ok": True, **res}


@app.post("/brd/requirement/revert")
def brd_requirement_revert(body: RequirementDeleteBody, user: dict = Depends(require_user)):
    """Safety net: restore a requirement to its previous text (undo the last edit)."""
    if not isinstance(body.chunk_id, int):
        return JSONResponse({"error": "missing chunk_id"}, status_code=400)
    try:
        res = revert_requirement(user["id"], body.chunk_id, changed_by=user["id"])
    except ChunkNotFound:
        return JSONResponse({"error": "requirement not found"}, status_code=404)
    except VoyageUnavailable as e:
        return JSONResponse({"error": f"Re-embedding is rate-limited right now: {e}"}, status_code=503)
    except psycopg.OperationalError as e:
        return _busy_error(e)
    return {"ok": True, **res}


@app.post("/brd/requirement/structure")
def brd_requirement_structure(body: RequirementStructureBody, user: dict = Depends(require_user)):
    """Propose row boundaries for a flattened table chunk (no persistence).

    Returns {rows, flattened} — `rows` are literal substrings of the chunk that
    reconstruct it exactly (the model only chooses boundaries; see
    ingest.structure), so the user can review/edit them before splitting."""
    if not isinstance(body.chunk_id, int):
        return JSONResponse({"error": "missing chunk_id"}, status_code=400)
    from backend.core.db import pool
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """select c.text from brd_chunk c join brd_document d on d.id = c.document_id
                   where c.id = %s and d.owner_id = %s""",
                (body.chunk_id, user["id"]),
            )
            row = cur.fetchone()
    except psycopg.OperationalError as e:
        return _busy_error(e)
    if not row:
        return JSONResponse({"error": "requirement not found"}, status_code=404)
    text = row[0] or ""
    try:
        proposal = req_structure.propose_rows(text, route=_edit_route(user, None))
    except psycopg.OperationalError as e:
        return _busy_error(e)
    except Exception as e:  # noqa: BLE001
        log.exception("requirement structure failed")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return {"ok": True, "flattened": req_structure.looks_flattened(text),
            "rows": proposal["rows"], "conserved": proposal["conserved"]}


@app.post("/brd/requirement/split")
def brd_requirement_split(body: RequirementSplitBody, user: dict = Depends(require_user)):
    """Persist a chunk split into per-row requirements (each independently editable)."""
    if not isinstance(body.chunk_id, int):
        return JSONResponse({"error": "missing chunk_id"}, status_code=400)
    rows = [r for r in (body.rows or []) if (r or "").strip()]
    if len(rows) < 2:
        return JSONResponse({"error": "need at least two rows to split"}, status_code=400)
    try:
        res = split_requirement(user["id"], body.chunk_id, rows, changed_by=user["id"])
    except ChunkNotFound:
        return JSONResponse({"error": "requirement not found"}, status_code=404)
    except VoyageUnavailable as e:
        return JSONResponse({"error": f"Re-embedding is rate-limited right now: {e}"}, status_code=503)
    except psycopg.OperationalError as e:
        return _busy_error(e)
    except Exception as e:  # noqa: BLE001
        log.exception("requirement split failed")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return {"ok": True, **res}


@app.get("/usage", summary="Live usage: free model requests left today, embedding capacity this minute")
def usage_status(refresh: bool = False, user: dict = Depends(require_user)):
    """What the user's chats and generation run on, and how much of it is left. OpenRouter keys
    report their daily free-model allowance live; other providers don't expose usage. The
    server key's status is shown too (it's the fallback, shared by everyone without a key) —
    counts only, never the key."""
    secret = llm_keys.get_secret(user["id"]) if crypto.available() else None
    own = None
    if secret:
        provider, base_url, api_key = secret
        own = llm_usage.openrouter_status(api_key, base_url, force=refresh) or {
            "available": False, "provider": provider,
            "error": "This provider doesn't report usage through its API — check its dashboard."}
    system = (llm_usage.openrouter_status(settings.openrouter_api_key, settings.openrouter_base_url, force=refresh)
              if settings.openrouter_api_key else None)
    return {"uses": "own" if secret else "system", "own_key": own, "system": system,
            "embeddings": llm_usage.embeddings_status()}


def _edit_route(user: dict, model_hint: str | None = None) -> change_agent.GenRoute | None:
    """Resolve the generation route for a requirement-edit call: the user's ACTIVE
    BYOK provider key + chosen model, mirroring how /ask routes generation. Returns
    None (⇒ system OpenRouter + GEN_MODEL) when no key is configured, or when a key
    is set but no model can be chosen (a BYOK key can't run the system model id)."""
    secret = llm_keys.get_secret(user["id"]) if crypto.available() else None
    if not secret:
        return None
    provider, base_url, api_key = secret
    model = (model_hint or "").strip() or None
    if not model:                          # fall back to the key's preferred, then any available model
        pref = llm_keys.get_preferred(user["id"])
        model = pref[0] if pref else next(iter(llm_keys.list_models(user["id"])), None)
    if not model:
        return None
    return change_agent.GenRoute(provider, api_key, base_url, model)


@app.post("/brd/requirement/plan")
def brd_requirement_plan(body: RequirementPlanBody, user: dict = Depends(require_user)):
    """Story-driven change: propose concrete edits/adds/deletes for a plain-language
    change, grounded in the BRD's own requirements (no persistence)."""
    project = (body.project or "").strip()
    story = (body.story or "").strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    if not story:
        return JSONResponse({"error": "describe the change first"}, status_code=400)
    if len(story) > 4000:
        return JSONResponse({"error": "change description too long (max 4000 chars)"}, status_code=413)
    try:
        plan = change_agent.plan_change(user["id"], project, story,
                                        route=_edit_route(user, body.model), refine=body.refine)
    except VoyageUnavailable as e:
        return JSONResponse({"error": f"Search is rate-limited right now: {e}"}, status_code=503)
    except psycopg.OperationalError as e:
        return _busy_error(e)
    except Exception as e:  # noqa: BLE001
        log.exception("requirement plan failed")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    if plan.get("error"):
        return JSONResponse({"error": plan["error"]}, status_code=400)
    return {"ok": True, **plan}


@app.post("/brd/requirement/apply")
def brd_requirement_apply(body: RequirementApplyBody, request: Request, user: dict = Depends(require_user)):
    """Apply the reviewed operations from a story-driven plan — atomically and
    idempotently (see ingest.edit.apply_change_set). A retried apply carrying the same
    Idempotency-Key (header or body) replays the stored result instead of re-applying."""
    project = (body.project or "").strip()
    ops = body.operations or []
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    if not ops:
        return JSONResponse({"error": "no changes to apply"}, status_code=400)
    key = (request.headers.get("Idempotency-Key") or body.idempotency_key or "").strip() or None
    try:
        res = change_agent.apply_operations(user["id"], project, ops, changed_by=user["id"],
                                            idempotency_key=key, story=body.story,
                                            refined=body.refined, model=body.model)
    except StaleRequirement as e:
        # The BRD changed since this plan was built — nothing was applied (atomic).
        return JSONResponse({"error": "This BRD changed since you planned these edits, so nothing was "
                             "applied. Re-run the change to see the current text.",
                             "conflict": True, "chunk_id": e.chunk_id}, status_code=409)
    except ChunkNotFound:
        return JSONResponse({"error": "A requirement in this plan no longer exists — nothing was applied. "
                             "Re-run the change against the current BRD.", "conflict": True}, status_code=409)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except VoyageUnavailable as e:
        return JSONResponse({"error": f"Re-embedding is rate-limited right now: {e}"}, status_code=503)
    except psycopg.OperationalError as e:
        return _busy_error(e)
    except Exception as e:  # noqa: BLE001
        log.exception("requirement apply failed")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return {"ok": True, **res}


@app.post("/brd/requirement/scope_edit")
def brd_requirement_scope_edit(body: RequirementScopeEditBody, user: dict = Depends(require_user)):
    """On-demand: propose a precise edit for ONE touched requirement (no persistence)."""
    if not isinstance(body.chunk_id, int):
        return JSONResponse({"error": "missing chunk_id"}, status_code=400)
    try:
        res = change_agent.propose_scope_edit(user["id"], body.chunk_id, body.change or "",
                                              route=_edit_route(user, body.model))
    except VoyageUnavailable as e:
        return JSONResponse({"error": f"Search is rate-limited right now: {e}"}, status_code=503)
    except psycopg.OperationalError as e:
        return _busy_error(e)
    except Exception as e:  # noqa: BLE001
        log.exception("scope edit failed")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    if res.get("error"):
        return JSONResponse({"error": res["error"]}, status_code=404 if "not found" in res["error"] else 500)
    return {"ok": True, **res}


@app.get("/brd/requirement/history", summary="Read the edit history of one requirement")
def brd_requirement_history(chunk_id: int, user: dict = Depends(require_scope("read"))):
    return {"history": requirement_history(user["id"], chunk_id)}


@app.get("/brd/requirement/changes", summary="Provenance feed of story-driven change requests")
def brd_requirement_changes(project: str = "", user: dict = Depends(require_scope("read"))):
    project = (project or "").strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    return {"changes": list_change_requests(user["id"], project)}


# --- Use cases (generated from a BRD, organized as a folder tree) -----------
_uc_jobs: dict[str, dict] = {}      # (owner:project) -> progress of a running generation
_uc_lock = threading.Lock()


def _uc_key(user: dict, project: str) -> str:
    return f"{user['id']}:{project}"


@app.get("/use-cases", summary="List the generated use-case tree for a BRD")
def use_cases_list(request: Request, project: str = "", user: dict = Depends(require_scope("read"))):
    project = (project or "").strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    enforce_project(request, user, project)
    return use_cases.get_tree(user["id"], project)


@app.get("/use-cases/status")
def use_cases_status(project: str = "", user: dict = Depends(require_user)):
    project = (project or "").strip()
    j = _uc_jobs.get(_uc_key(user, project))
    if j and j.get("running"):
        return j
    # Idle: report ledger-derived resume state so the UI can offer Resume vs Regenerate
    # even after a server restart (the in-memory job is gone, the ledger is not).
    resume = use_cases.resume_state(user["id"], project) if project else {}
    return {**(j or {"running": False}), "idle": not (j and j.get("running")), "resume": resume}


@app.post("/use-cases/generate")
def use_cases_generate(body: UseCaseGenerateBody, user: dict = Depends(require_user)):
    """Start a background job that derives the use-case tree for a BRD, scope by scope
    (progress via GET /use-cases/status; the tree fills in as scopes complete)."""
    project = (body.project or "").strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    key = _uc_key(user, project)
    # Route generation through the user's ACTIVE BYOK key + the model chosen in the chat
    # (same resolution as /ask); None ⇒ system OpenRouter + GEN_MODEL. Resolved per request,
    # so switching models regenerates on the newly-selected one.
    route = _edit_route(user, body.model)
    with _uc_lock:
        job = _uc_jobs.get(key)
        if job and job.get("running"):
            return JSONResponse({"error": "Generation already in progress."}, status_code=409)
        _uc_jobs[key] = {"running": True, "done": 0, "total": 0, "scope": None, "made": 0, "error": None}

    def _progress(done: int, total: int, scope: str | None) -> None:
        with _uc_lock:
            j = _uc_jobs.get(key)
            if j:
                j.update(done=done, total=total, scope=scope)

    def run() -> None:
        try:
            res = use_cases.generate_for_project(user["id"], project, route=route,
                                                 replace=body.replace, mode=body.mode,
                                                 coverage=(body.coverage or "functional"),
                                                 on_progress=_progress)
            with _uc_lock:
                _uc_jobs[key].update(running=False, made=res.get("use_cases", 0),
                                     error=res.get("error"), errors=res.get("errors") or [])
        except Exception as e:  # noqa: BLE001
            log.exception("use-case generation failed")
            with _uc_lock:
                _uc_jobs[key].update(running=False, error=f"{type(e).__name__}: {e}")

    threading.Thread(target=run, daemon=True).start()
    return {"started": True}


# --- Use case editing (QA refines the tree; human-only) --------------------
@app.post("/use-cases/update")
def use_case_update(body: UseCaseUpdateBody, user: dict = Depends(require_user)):
    if not isinstance(body.id, int):
        return JSONResponse({"error": "missing id"}, status_code=400)
    fields = body.model_dump(exclude_none=True)
    fields.pop("id", None)
    if not fields:
        return JSONResponse({"error": "nothing to update"}, status_code=400)
    if not use_cases.update_use_case(user["id"], body.id, fields, changed_by=user["id"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


@app.post("/use-cases/delete")
def use_case_delete(body: UseCaseIdBody, user: dict = Depends(require_user)):
    if not isinstance(body.id, int) or not use_cases.delete_use_case(user["id"], body.id, changed_by=user["id"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


@app.post("/use-cases/move")
def use_case_move(body: UseCaseMoveBody, user: dict = Depends(require_user)):
    if not isinstance(body.id, int) or not isinstance(body.folder_id, int):
        return JSONResponse({"error": "missing id or folder_id"}, status_code=400)
    if not use_cases.move_use_case(user["id"], body.id, body.folder_id, changed_by=user["id"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


# --- Use-case TEST EXECUTION (status lifecycle + evidence + cycles; see docs/USECASE_TESTING.md) ---
# Every write returns the resulting event + the card's new state, so the UI patches in place
# instead of refetching the timeline and the whole tree.
def _bad(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=code)


@app.post("/use-cases/test-status", summary="Record a verdict or note (claims staged evidence)")
def use_case_test_status(body: UseCaseTestStatusBody, user: dict = Depends(require_user)):
    if not isinstance(body.id, int):
        return _bad("missing id")
    try:
        res = uct.record(user["id"], body.id, event_kind=(body.event_kind or "status"), status=body.status,
                         note=body.note, defect_ref=body.defect_ref, cycle_id=body.cycle_id, by=user["id"])
    except uct.TestingError as e:
        return _bad(str(e))
    if res is None:
        return _bad("not found", 404)
    return {"ok": True, **res}


@app.post("/use-cases/test-status/bulk", summary="Record one verdict on many use cases")
def use_case_test_status_bulk(body: UseCaseTestBulkBody, user: dict = Depends(require_user)):
    if not body.ids:
        return _bad("no use cases selected")
    try:
        cards = uct.record_bulk(user["id"], body.ids, body.status or "", note=body.note,
                                cycle_id=body.cycle_id, by=user["id"])
    except uct.TestingError as e:
        return _bad(str(e))
    return {"ok": True, "cards": cards}


@app.post("/use-cases/test-status/undo", summary="Undo the latest test entry (within the window)")
def use_case_test_undo(body: UseCaseTestUndoBody, user: dict = Depends(require_user)):
    if not isinstance(body.result_id, int):
        return _bad("missing result_id")
    try:
        res = uct.undo(user["id"], body.result_id, cycle_id=body.cycle_id, by=user["id"])
    except uct.TestingError as e:
        return _bad(str(e))
    if res is None:
        return _bad("not found", 404)
    return {"ok": True, **res}


@app.post("/use-cases/test-status/edit", summary="Correct the latest entry's note / defect")
def use_case_test_edit(body: UseCaseTestEditBody, user: dict = Depends(require_user)):
    if not isinstance(body.result_id, int):
        return _bad("missing result_id")
    try:
        res = uct.edit_event(user["id"], body.result_id, note=body.note, defect_ref=body.defect_ref, by=user["id"])
    except uct.TestingError as e:
        return _bad(str(e))
    if res is None:
        return _bad("not found", 404)
    return {"ok": True, **res}


@app.get("/use-cases/{uid}/timeline", summary="Full test-execution timeline for a use case")
def use_case_timeline(uid: int, cycle_id: int | None = None, user: dict = Depends(require_user)):
    return uct.timeline(user["id"], uid, cycle_id)


@app.post("/use-cases/scope", summary="Toggle a use case in/out of scope (separate from the verdict)")
def use_case_scope(body: UseCaseScopeBody, user: dict = Depends(require_user)):
    if not isinstance(body.id, int):
        return _bad("missing id")
    res = uct.set_scope(user["id"], body.id, bool(body.in_scope), note=body.note,
                        cycle_id=body.cycle_id, by=user["id"])
    if res is None:
        return _bad("not found", 404)
    return {"ok": True, **res}


@app.post("/use-cases/scope/bulk", summary="Toggle scope on many use cases")
def use_case_scope_bulk(body: UseCaseScopeBulkBody, user: dict = Depends(require_user)):
    if not body.ids:
        return _bad("no use cases selected")
    return {"ok": True, "cards": uct.set_scope_bulk(user["id"], body.ids, bool(body.in_scope), by=user["id"])}


@app.post("/use-cases/proof/link", summary="Stage an external-link proof (http/https only)")
def use_case_proof_link(body: UseCaseProofLinkBody, user: dict = Depends(require_user)):
    if not isinstance(body.id, int):
        return _bad("missing id")
    try:
        res = uct.stage_proof(user["id"], body.id, url=body.url, caption=body.caption, by=user["id"])
    except uct.TestingError as e:
        return _bad(str(e))
    if res is None:
        return _bad("not found", 404)
    return {"ok": True, "proof": res}


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """Read a raw body, refusing it BEFORE buffering when it's declared or grows too large."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            return None
    return bytes(buf)


@app.post("/use-cases/proof/file", summary="Stage a file proof (raw body; metadata in headers)")
async def use_case_proof_file(request: Request, user: dict = Depends(require_user)):
    try:
        uc_id = int(request.headers.get("X-UC-Id", ""))
    except ValueError:
        return _bad("missing X-UC-Id")
    data = await _read_capped(request, uct.MAX_PROOF_BYTES)
    if data is None:
        return _bad(f"file too large (max {uct.MAX_PROOF_BYTES // (1024 * 1024)} MB)", 413)
    if not data:
        return _bad("empty upload")
    filename = unquote(request.headers.get("X-Filename", "")) or None
    caption = unquote(request.headers.get("X-Caption", "")) or None
    try:
        res = uct.stage_proof(user["id"], uc_id, filename=filename, mime=request.headers.get("Content-Type"),
                              content=data, caption=caption, by=user["id"])
    except uct.TestingError as e:
        return _bad(str(e))
    if res is None:
        return _bad("not found", 404)
    return {"ok": True, "proof": res}


@app.get("/use-cases/proof/{proof_id}", summary="Fetch a file proof's bytes")
def use_case_proof_blob(proof_id: int, download: int = 0, user: dict = Depends(require_user)):
    got = uct.get_proof_blob(user["id"], proof_id)
    if got is None:
        return _bad("not found", 404)
    blob, mime, filename = got
    # Only passive media renders inline; anything else (html, svg, js…) is a download, so an
    # uploaded document can never run script on this origin. Proofs are immutable per id.
    inline = uct.inline_ok(mime) and not download
    headers = {
        "Content-Disposition": uct.content_disposition(filename, inline),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=31536000, immutable",
    }
    if mime != "application/pdf":   # the browser PDF viewer refuses to run under a sandbox CSP
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'; media-src 'self'"
    return Response(content=blob, media_type=mime if inline else "application/octet-stream", headers=headers)


@app.post("/use-cases/proof/delete", summary="Delete a staged proof, or one on the latest event")
def use_case_proof_delete(body: UseCaseProofIdBody, user: dict = Depends(require_user)):
    if not isinstance(body.proof_id, int):
        return _bad("missing proof_id")
    try:
        ok = uct.delete_proof(user["id"], body.proof_id, by=user["id"])
    except uct.TestingError as e:
        return _bad(str(e))
    if not ok:
        return _bad("not found", 404)
    return {"ok": True}


@app.post("/use-cases/proof/caption", summary="Relabel a proof")
def use_case_proof_caption(body: UseCaseProofIdBody, user: dict = Depends(require_user)):
    if not isinstance(body.proof_id, int):
        return _bad("missing proof_id")
    if not uct.set_caption(user["id"], body.proof_id, body.caption, by=user["id"]):
        return _bad("not found", 404)
    return {"ok": True}


@app.get("/use-cases/report", summary="Coverage + traceability report (overall or for one cycle)")
def use_cases_report(request: Request, project: str = "", cycle_id: int | None = None,
                     user: dict = Depends(require_scope("read"))):
    project = (project or "").strip()
    if not project:
        return _bad("missing project")
    enforce_project(request, user, project)
    try:
        return uct.report(user["id"], project, cycle_id)
    except uct.TestingError as e:
        return _bad(str(e), 404)


# --- test cycles (named runs → per-cycle status, traceability) ---
@app.get("/test-cycles", summary="Test cycles of a BRD with live progress")
def test_cycles(request: Request, project: str = "", user: dict = Depends(require_scope("read"))):
    project = (project or "").strip()
    if not project:
        return _bad("missing project")
    enforce_project(request, user, project)
    return {"cycles": uct.list_cycles(user["id"], project)}


@app.post("/test-cycles/create", summary="Create a test cycle and plan its cases")
def test_cycle_create(request: Request, body: TestCycleCreateBody, user: dict = Depends(require_user)):
    project = (body.project or "").strip()
    if not project:
        return _bad("missing project")
    enforce_project(request, user, project)
    try:
        cycle = uct.create_cycle(user["id"], project, body.name, description=body.description,
                                 source=body.source, case_types=body.case_types, folder_ids=body.folder_ids,
                                 from_cycle_id=body.from_cycle_id, carry=body.carry, by=user["id"])
    except uct.TestingError as e:
        return _bad(str(e))
    return {"ok": True, "cycle": cycle}


@app.post("/test-cycles/update", summary="Rename / describe / close / reopen a test cycle")
def test_cycle_update(body: TestCycleUpdateBody, user: dict = Depends(require_user)):
    if not isinstance(body.cycle_id, int):
        return _bad("missing cycle_id")
    try:
        cycle = uct.update_cycle(user["id"], body.cycle_id, name=body.name,
                                 description=body.description, status=body.status)
    except uct.TestingError as e:
        return _bad(str(e))
    if cycle is None:
        return _bad("not found", 404)
    return {"ok": True, "cycle": cycle}


@app.post("/test-cycles/delete", summary="Delete a test cycle (its results are kept as ad-hoc)")
def test_cycle_delete(body: TestCycleUpdateBody, user: dict = Depends(require_user)):
    if not isinstance(body.cycle_id, int):
        return _bad("missing cycle_id")
    if not uct.delete_cycle(user["id"], body.cycle_id):
        return _bad("not found", 404)
    return {"ok": True}


@app.post("/test-cycles/members", summary="Plan use cases into / out of a test cycle")
def test_cycle_members(body: TestCycleMembersBody, user: dict = Depends(require_user)):
    if not isinstance(body.cycle_id, int):
        return _bad("missing cycle_id")
    res = uct.set_cycle_members(user["id"], body.cycle_id, add_uc_ids=body.add_ids, remove_uids=body.remove_uids,
                                by=user["id"])
    if res is None:
        return _bad("not found", 404)
    return {"ok": True, **res}


@app.get("/test-cycles/compare", summary="Run-over-run comparison: regressions, fixes, not re-run")
def test_cycles_compare(request: Request, project: str = "", base: int | None = None, target: int | None = None,
                        user: dict = Depends(require_scope("read"))):
    project = (project or "").strip()
    if not project or base is None or target is None:
        return _bad("missing project, base or target")
    enforce_project(request, user, project)
    try:
        return uct.compare_cycles(user["id"], project, base, target)
    except uct.TestingError as e:
        return _bad(str(e), 404)


@app.get("/test-cycles/{cycle_id}/statuses", summary="Per-case status within a test cycle")
def test_cycle_statuses(cycle_id: int, user: dict = Depends(require_user)):
    res = uct.cycle_statuses(user["id"], cycle_id)
    if res is None:
        return _bad("not found", 404)
    return res


# --- protected regeneration: change proposals for TESTED use cases (docs §5) ---
@app.get("/use-cases/versions", summary="Published versions of a BRD's use cases + the draft's pending changes")
def use_case_versions(request: Request, project: str = "", user: dict = Depends(require_scope("read"))):
    project = (project or "").strip()
    if not project:
        return _bad("missing project")
    enforce_project(request, user, project)
    return {**ucsuites.status(user["id"], project), "versions": ucsuites.list_versions(user["id"], project)}


@app.get("/use-cases/versions/diff", summary="Compare a version with another version or with the draft")
def use_case_versions_diff(request: Request, project: str = "", base: int | None = None, target: str = "draft",
                           user: dict = Depends(require_scope("read"))):
    project = (project or "").strip()
    if not project:
        return _bad("missing project")
    enforce_project(request, user, project)
    if target != "draft" and not target.isdigit():
        return _bad("target must be a version id or 'draft'")
    try:
        return ucsuites.diff(user["id"], project, base, target if target == "draft" else int(target))
    except ucsuites.SuiteError as e:
        return _bad(str(e), 404)


@app.get("/use-cases/versions/{version_id}/case/{uid}", summary="One case exactly as a version defines it")
def use_case_version_case(request: Request, version_id: int, uid: int, project: str = "",
                          user: dict = Depends(require_scope("read"))):
    project = (project or "").strip()
    if not project:
        return _bad("missing project")
    enforce_project(request, user, project)
    try:
        return ucsuites.case_in_version(user["id"], project, version_id, uid)
    except ucsuites.SuiteError as e:
        return _bad(str(e), 404)


@app.post("/use-cases/versions/publish", summary="Freeze the draft as the next immutable version")
def use_case_versions_publish(request: Request, body: SuitePublishBody, user: dict = Depends(require_user)):
    project = (body.project or "").strip()
    if not project:
        return _bad("missing project")
    enforce_project(request, user, project)
    if _generation_running(user, project):
        return _bad("Use cases are being generated or updated — publish once that finishes.", 409)
    try:
        return {"ok": True, **ucsuites.publish(user["id"], project, user["id"], body.label, body.expect)}
    except ucsuites.Stale as e:
        return _bad(str(e), 409)
    except ucsuites.SuiteError as e:
        return _bad(str(e))


def _generation_running(user: dict, project: str) -> bool:
    """A generation/update job writes the draft batch by batch — publishing or restoring in the
    middle would freeze or clobber a half-updated suite."""
    with _uc_lock:
        j = _uc_jobs.get(_uc_key(user, project))
        return bool(j and j.get("running"))


@app.post("/use-cases/versions/restore", summary="Make the draft equal to a published version (then publish it)")
def use_case_versions_restore(request: Request, body: SuiteRestoreBody, user: dict = Depends(require_user)):
    project = (body.project or "").strip()
    if not project or not isinstance(body.version_id, int):
        return _bad("missing project or version_id")
    enforce_project(request, user, project)
    if _generation_running(user, project):
        return _bad("Use cases are being generated or updated — restore once that finishes.", 409)
    try:
        return {"ok": True, **use_cases.restore_version(user["id"], project, body.version_id, user["id"])}
    except ucsuites.SuiteError as e:
        return _bad(str(e), 404)


@app.post("/use-cases/versions/label", summary="Rename a version (the only editable part of a version)")
def use_case_versions_label(request: Request, body: SuiteLabelBody, user: dict = Depends(require_user)):
    project = (body.project or "").strip()
    if not project or not isinstance(body.version_id, int):
        return _bad("missing project or version_id")
    enforce_project(request, user, project)
    try:
        return {"ok": True, "version": ucsuites.set_label(user["id"], project, body.version_id, body.label)}
    except ucsuites.SuiteError as e:
        return _bad(str(e), 404)


@app.get("/use-cases/{uid}/audit", summary="Append-only audit log of one use case (who / what / when / status)")
def use_case_audit_log(uid: int, category: str | None = None, user: dict = Depends(require_user)):
    return ucaudit.entries(user["id"], uid, category)


@app.get("/use-cases/{uid}/history")
def use_case_history(uid: int, user: dict = Depends(require_user)):
    """Version timeline for one use case (by stable uid); owner-scoped."""
    return ucv.history(user["id"], uid)


@app.get("/use-cases/{uid}/history/{version_no}")
def use_case_history_diff(uid: int, version_no: int, user: dict = Depends(require_user)):
    """Field-level before→after for one version vs. its predecessor (audit 'what changed')."""
    return ucv.diff(user["id"], uid, version_no)


@app.post("/use-cases/restore")
def use_case_restore(body: UseCaseRestoreBody, user: dict = Depends(require_user)):
    """Restore a use case to a prior version (re-inserting it if it had been deleted)."""
    if not isinstance(body.uid, int) or not isinstance(body.version_no, int):
        return JSONResponse({"error": "missing uid or version_no"}, status_code=400)
    res = ucv.restore(user["id"], body.uid, body.version_no, changed_by=user["id"])
    if not res.get("restored"):
        return JSONResponse({"error": res.get("reason", "restore failed")}, status_code=404)
    return res


@app.get("/use-cases/deleted")
def use_cases_deleted(request: Request, project: str = "", user: dict = Depends(require_scope("read"))):
    """Cards with history but no live row (deleted or superseded) — the Trash / backup view."""
    project = (project or "").strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    enforce_project(request, user, project)
    return {"deleted": ucv.list_deleted(user["id"], project)}


@app.post("/use-cases/folder/create")
def uc_folder_create(body: UcFolderCreateBody, user: dict = Depends(require_user)):
    project = (body.project or "").strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    folder = use_cases.create_folder(user["id"], project, body.parent_id, body.name)
    if folder is None:
        return JSONResponse({"error": "invalid name or parent"}, status_code=400)
    return {"ok": True, "folder": folder}


@app.post("/use-cases/folder/rename")
def uc_folder_rename(body: UcFolderRenameBody, user: dict = Depends(require_user)):
    if not isinstance(body.folder_id, int) or not use_cases.rename_folder(user["id"], body.folder_id, body.name):
        return JSONResponse({"error": "not found or empty name"}, status_code=404)
    return {"ok": True}


@app.post("/use-cases/folder/delete")
def uc_folder_delete(body: UcFolderIdBody, user: dict = Depends(require_user)):
    if not isinstance(body.folder_id, int) or not use_cases.delete_folder(user["id"], body.folder_id, changed_by=user["id"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


# --- versioning + approval (draft edit → review → live) ---------------------
@app.get("/brd/versions", summary="List a BRD's saved version snapshots")
def brd_versions(request: Request, project: str = "", user: dict = Depends(require_scope("read"))):
    project = (project or "").strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    enforce_project(request, user, project)
    try:
        return versioning.list_versions(user["id"], project)
    except versioning.DocNotFound:
        return JSONResponse({"error": "BRD not found"}, status_code=404)


@app.post("/brd/version/snapshot")
def brd_snapshot(body: VersionSnapshotBody, user: dict = Depends(require_user)):
    project = (body.project or "").strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    try:
        return {"ok": True, **versioning.snapshot(user["id"], project, body.label, created_by=user["id"])}
    except versioning.DocNotFound:
        return JSONResponse({"error": "BRD not found"}, status_code=404)


@app.post("/brd/version/restore")
def brd_restore(body: VersionRestoreBody, user: dict = Depends(require_user)):
    project = (body.project or "").strip()
    if not project or not isinstance(body.snapshot_id, int):
        return JSONResponse({"error": "missing project or snapshot_id"}, status_code=400)
    try:
        return {"ok": True, **versioning.restore(user["id"], project, body.snapshot_id, created_by=user["id"])}
    except versioning.DocNotFound:
        return JSONResponse({"error": "BRD or snapshot not found"}, status_code=404)


@app.post("/brd/approve")
def brd_approve(body: ProjectActionBody, user: dict = Depends(require_user)):
    project = (body.project or "").strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    try:
        return {"ok": True, **versioning.approve(user["id"], project, body.label, created_by=user["id"])}
    except versioning.DocNotFound:
        return JSONResponse({"error": "BRD not found"}, status_code=404)


@app.post("/brd/discard")
def brd_discard(body: ProjectActionBody, user: dict = Depends(require_user)):
    project = (body.project or "").strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    try:
        return {"ok": True, **versioning.discard(user["id"], project, created_by=user["id"])}
    except versioning.DocNotFound:
        return JSONResponse({"error": "BRD not found"}, status_code=404)


@app.post("/upload", summary="Upload a BRD (.pdf/.docx) — a new BRD, or a new version of one")
async def upload(request: Request, user: dict = Depends(require_user)):
    """Stores the file in object storage and queues a durable ingest job; returns at once.
    Headers: X-Filename (required), X-Title (optional display title), X-Project (upload a NEW
    VERSION of this existing BRD — processed, compared and staged for review), X-As-New: 1
    (keep as a separate BRD when the title clashes with an existing one). A title clash
    without X-As-New answers 409 {conflict} so the UI can ask the user what they intend."""
    filename = _sanitize_header(unquote(request.headers.get("X-Filename", "")))
    title = _sanitize_header(unquote(request.headers.get("X-Title", ""))) or None
    target = _sanitize_header(unquote(request.headers.get("X-Project", ""))) or None
    as_new = request.headers.get("X-As-New", "") in ("1", "true")
    if not filename:
        return _bad("missing X-Filename")
    data = await _read_capped(request, settings.ingest_max_bytes)
    if data is None:
        return _bad(f"file too large (max {settings.ingest_max_bytes // (1024 * 1024)} MB)", 413)
    try:
        return ingest_jobs.submit(user["id"], user["id"], data, filename, title=title,
                                  target_project=target, as_new=as_new)
    except ingest_jobs.UploadRejected as e:
        return JSONResponse({"error": str(e), **e.payload}, status_code=e.status)


# --- ingestion jobs + revisions (docs/INGESTION.md) ---
@app.get("/ingest/jobs", summary="Recent ingestion jobs (optionally for one BRD)")
def ingest_job_list(project: str = "", user: dict = Depends(require_user)):
    return {"jobs": ingest_jobs.jobs(user["id"], (project or "").strip() or None)}


class IngestJobBody(BaseModel):
    job_id: int | None = None


@app.post("/ingest/jobs/retry", summary="Retry a failed ingestion job")
def ingest_job_retry(body: IngestJobBody, user: dict = Depends(require_user)):
    if not isinstance(body.job_id, int):
        return _bad("missing job_id")
    if ingest_jobs.retry(user["id"], body.job_id):
        return {"ok": True}
    # job not found for this owner, not in a 'failed' state, or already superseded by another
    # queued/running job for the same BRD (the active-job constraint) — a conflict, not a 404:
    # the id itself may be perfectly valid, just not retryable right now. Distinguish the two
    # so the message is actionable instead of a generic "nothing to retry".
    from backend.core.db import pool as _pool
    with _pool().connection() as conn, conn.cursor() as cur:
        cur.execute("select document_id from ingest_job where id = %s and owner_id = %s",
                    (body.job_id, user["id"]))
        row = cur.fetchone()
        if row:
            cur.execute("select 1 from ingest_job where document_id = %s and status in ('queued', 'running')",
                        (row[0],))
            if cur.fetchone():
                return _bad("This BRD already has a job in progress.", 409)
    return _bad("nothing to retry", 409)


@app.post("/ingest/jobs/cancel", summary="Cancel a queued or running ingestion job")
def ingest_job_cancel(body: IngestJobBody, user: dict = Depends(require_user)):
    if not isinstance(body.job_id, int) or not ingest_jobs.cancel(user["id"], body.job_id):
        return _bad("nothing to cancel", 404)
    return {"ok": True}


class ProjectBody(BaseModel):
    project: str = ""


@app.post("/ingest/reprocess", summary="Re-process a BRD from its stored source (as a revision to review)")
def ingest_reprocess(body: ProjectBody, user: dict = Depends(require_user)):
    try:
        return ingest_jobs.reprocess(user["id"], user["id"], (body.project or "").strip())
    except ingest_jobs.UploadRejected as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)


@app.get("/brd/source/{source_id}", summary="Download a BRD's original uploaded file")
def brd_source_download(source_id: int, user: dict = Depends(require_user)):
    try:
        got = ingest_jobs.source_blob(user["id"], source_id)
    except Exception:  # noqa: BLE001
        return _bad("file storage is unavailable", 503)
    if got is None:
        return _bad("not found", 404)
    blob, _mime, filename = got
    return Response(content=blob, media_type="application/octet-stream",
                    headers={"Content-Disposition": uct.content_disposition(filename, False),
                             "X-Content-Type-Options": "nosniff"})


@app.get("/brd/revision", summary="The BRD's new version awaiting review (compare view)")
def brd_revision_get(project: str = "", user: dict = Depends(require_user)):
    return {"revision": brd_revise.get_open(user["id"], (project or "").strip())}


class RevisionApplyBody(BaseModel):
    revision_id: int | None = None
    keep_removed: list[int] = []     # item ids of removed requirements to KEEP
    skip: list[int] = []             # item ids of modified / added changes NOT to apply
    confirm_replace: bool = False    # user confirmed a large/ambiguous replacement after review


@app.post("/brd/revision/apply", summary="Apply a reviewed new version (ids preserved; removals archived)")
def brd_revision_apply(body: RevisionApplyBody, user: dict = Depends(require_user)):
    if not isinstance(body.revision_id, int):
        return _bad("missing revision_id")
    try:
        res = brd_revise.apply_revision(user["id"], body.revision_id, keep_removed=body.keep_removed,
                                        skip=body.skip, by=user["id"], confirm_replace=body.confirm_replace)
    except brd_revise.NeedsConfirmation as e:
        # The diff looks like a wholesale replacement — ask the user to confirm before
        # archiving that much of the live BRD (confirm_replace=true replays the same call).
        return JSONResponse({"error": str(e), "confirm_required": True, "summary": e.summary}, status_code=409)
    except brd_revise.Stale as e:
        return JSONResponse({"error": str(e), "stale": True}, status_code=409)
    except brd_revise.RevisionError as e:
        return _bad(str(e))
    except psycopg.OperationalError as e:
        return _busy_error(e)
    try:
        from backend.chat.suggest import invalidate
        invalidate(res["project"], user["id"])
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, **res}


class RevisionIdBody(BaseModel):
    revision_id: int | None = None


@app.post("/brd/revision/discard", summary="Discard a new version (the live BRD is untouched)")
def brd_revision_discard(body: RevisionIdBody, user: dict = Depends(require_user)):
    if not isinstance(body.revision_id, int):
        return _bad("missing revision_id")
    try:
        return {"ok": True, **brd_revise.discard_revision(user["id"], body.revision_id, by=user["id"])}
    except brd_revise.RevisionError as e:
        return _bad(str(e))
    except psycopg.OperationalError as e:
        return _busy_error(e)


@app.post("/brd/revision/recompare", summary="Re-compare a staged version against the current BRD")
def brd_revision_recompare(body: RevisionIdBody, user: dict = Depends(require_user)):
    if not isinstance(body.revision_id, int):
        return _bad("missing revision_id")
    try:
        return {"ok": True, **brd_revise.recompare(user["id"], body.revision_id)}
    except brd_revise.RevisionError as e:
        return _bad(str(e))
    except psycopg.OperationalError as e:
        return _busy_error(e)


@app.post("/ask", summary="Ask a question and get a cited answer about a granted BRD")
def ask(request: Request, body: AskBody, user: dict = Depends(require_scope("ask"))):
    question = (body.question or "").strip()
    if not question:
        return JSONResponse({"error": "empty question"}, status_code=400)
    if len(question) > MAX_QUESTION_CHARS:
        return JSONResponse({"error": f"question too long (max {MAX_QUESTION_CHARS} chars)"}, status_code=413)

    cid = body.conversation_id
    if isinstance(cid, int):
        if not _owns_conversation(cid, user):
            return JSONResponse({"error": "not found"}, status_code=404)
    else:
        cid = _new_conversation(user)

    # One ACTIVE turn per conversation. A canceled generation counts as free — its
    # worker keeps winding down in the background but won't persist, so a new turn
    # (e.g. a promoted queued prompt) can start immediately without a 409.
    live = _LiveGen(question)
    with _lock:
        existing = _live.get(cid)
        busy = existing is not None and not existing.canceled
        if not busy:
            _live[cid] = live
    if busy:
        return JSONResponse(
            {"error": "Still answering your previous question — one moment."},
            status_code=409,
        )

    sess = _session_for(cid, user)
    req_project = body.project or None
    if sess.project is None and req_project:
        set_conversation_project(cid, req_project)
        sess.project = req_project
    # Service tokens may only ask about a granted BRD (no-op for humans).
    enforce_project(request, user, sess.project)

    # Bring-your-own-key generation: route this turn through the user's provider key
    # + chosen model. Resolved per-request so key/model changes take effect immediately;
    # falls back to the system GEN_MODEL when no key or model is set.
    secret = llm_keys.get_secret(user["id"]) if crypto.available() else None
    model = (body.model or "").strip() or None
    if model is None:
        conv = get_conversation(cid)
        model = conv.get("model") if conv else None
    if secret and model:
        sess.gen_provider, sess.gen_base_url, sess.gen_key = secret
        sess.gen_model = model
        set_conversation_model(cid, model)   # remember for reload / next turn
    else:
        sess.gen_provider = sess.gen_base_url = sess.gen_key = sess.gen_model = None

    def _release() -> None:
        # Only clear the registry slot if it's still OURS — a cancel may have
        # already replaced us with a newer generation for this conversation.
        with _lock:
            if _live.get(cid) is live:
                _live.pop(cid, None)

    def run() -> None:
        try:
            res = sess.ask(question, on_token=live.emit, should_cancel=lambda: live.canceled)
            # A cancel that arrived while we were mid-provider-call still wins here.
            live.finish("canceled") if live.canceled else live.finish("done", result=_done_payload(cid, res))
        except GenerationCanceled:
            live.finish("canceled")   # user hit Stop — nothing was persisted
        except VoyageUnavailable as e:
            # Search/embeddings side (Voyage) is throttled — distinct from the LLM.
            live.finish("canceled") if live.canceled else live.finish(
                "error", error=f"Search is temporarily rate-limited (embeddings) — retry shortly. ({e})")
        except ProviderError as e:
            # Any LLM provider error (429/402/401/404/400/timeout) already carries a clear,
            # user-facing message. If this turn was canceled, its incidental error must NOT be reported.
            live.finish("canceled") if live.canceled else live.finish("error", error=str(e))
        except Exception as e:  # noqa: BLE001
            if live.canceled:
                live.finish("canceled")
            else:
                log.exception("ask failed")
                live.finish("error", error=f"Something went wrong generating the answer. ({type(e).__name__})")
        finally:
            _release()

    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception:            # thread never started → don't leak the registry slot
        _release()
        raise

    # The requester subscribes to the same live gen everyone else can re-attach to.
    return StreamingResponse(
        _stream_live(live),
        media_type="application/x-ndjson; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/ask/cancel")
def ask_cancel(request: Request, body: DeleteBody, user: dict = Depends(require_scope("ask"))):
    """Stop an in-flight answer for a conversation — the worker bails out and the
    turn is NOT persisted, so a canceled answer never reappears on reload."""
    cid = body.conversation_id
    if not isinstance(cid, int) or not _owns_conversation(cid, user):
        return JSONResponse({"error": "not found"}, status_code=404)
    _enforce_conversation_project(request, user, cid)
    with _lock:
        live = _live.get(cid)
    if live is not None:
        live.cancel()
    return {"ok": True}


def main() -> None:
    import uvicorn
    host = os.getenv("BRD_HOST", "127.0.0.1")
    # PORT is the platform-injected port (Render/Heroku/Cloud Run); BRD_PORT is our
    # own convention; 8000 is the local default. Prefer the platform's when present.
    port = int(os.getenv("PORT") or os.getenv("BRD_PORT") or "8000")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()

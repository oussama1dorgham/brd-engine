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
import threading
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import (
    FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.auth import (
    EmailTaken, RESET_TTL_HOURS, VERIFY_TTL_HOURS, SESSION_COOKIE,
    authenticate, clear_session_cookie, consume_token, create_session, create_token,
    create_user, current_user, delete_session, delete_user_sessions, mark_verified,
    rate_limit, require_user, set_password, set_session_cookie, user_by_email,
)
from backend import crypto, email_outbox, llm_keys, otp, versioning
from backend.config import settings
from backend.email_send import app_base_url, build_code, build_reset, build_verification
from backend.generate.history_store import (
    conversation_user_id, create_conversation, delete_conversation, delete_project,
    get_conversation, get_messages, list_conversations, list_projects, rename_project,
    set_conversation_model, set_conversation_project,
)
from backend.generate.session import ChatSession, GenerationCanceled
from backend.ingest.edit import (
    ChunkNotFound, add_requirement, list_requirements, remove_requirement,
    requirement_history, revert_requirement, split_requirement, update_requirement,
)
from backend.ingest import structure as req_structure
from backend.generate import change_agent
from backend.voyage import VoyageUnavailable
from backend.providers import registry as provider_registry
from backend.providers.base import ProviderError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("brd.api")

MAX_QUESTION_CHARS = 2000
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

app = FastAPI(title="BRD Retrieval Engine")

if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")


def _fail_stuck_processing() -> None:
    """A BRD left 'processing' from a previous run has a dead worker (in-process jobs
    don't survive a restart) — mark it 'failed' so the UI shows it and the user can re-upload."""
    try:
        from backend.db import pool
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("update brd_document set status = 'failed' where status = 'processing'")
            if cur.rowcount:
                log.info("startup: marked %d stuck 'processing' BRD(s) as failed", cur.rowcount)
            conn.commit()
    except Exception:  # noqa: BLE001
        log.exception("startup: failed to reset stuck processing BRDs")


_fail_stuck_processing()
email_outbox.reclaim_stuck()   # re-arm any email left 'sending' by a dead worker
email_outbox.start_sweeper()   # background fixed-interval retry of failed sends

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
            {"n": s["n"], "req_id": s["req_id"], "section": s["section"],
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


class RequirementApplyBody(BaseModel):
    project: str = ""
    operations: list[dict] = []


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


@app.get("/health")
def health():
    ok = True
    try:
        from backend.db import pool
        with pool().connection() as conn, conn.cursor() as cur:
            cur.execute("select 1")
            cur.fetchone()
    except Exception:  # noqa: BLE001
        ok = False
    return JSONResponse({"status": "ok" if ok else "degraded", "db": ok}, status_code=200 if ok else 503)


@app.get("/conversations")
def conversations(user: dict = Depends(require_user), limit: int = 30, before: int | None = None):
    """A page of the user's conversations (newest first). `before` = the last id
    you've seen (keyset cursor); `has_more` tells the client to keep lazy-loading."""
    limit = max(1, min(limit, 100))
    rows = list_conversations(str(user["id"]), limit + 1, before)   # +1 to detect more
    has_more = len(rows) > limit
    return {"conversations": rows[:limit], "has_more": has_more}


@app.get("/conversation/{cid}")
def conversation(cid: int, user: dict = Depends(require_user)):
    # cid is a path param (FastAPI validates it as int → 422 on garbage).
    if not _owns_conversation(cid, user):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"messages": get_messages(cid)}


@app.get("/conversation/{cid}/stream")
def conversation_stream(cid: int, user: dict = Depends(require_user)):
    """Re-attach to an answer still being generated for this conversation.

    Streams `{question}` (so the reopened UI can render the pending turn), then
    the tokens produced so far, then the rest live + the final payload. If nothing
    is generating, emits `{"idle": true}` and the client shows persisted messages.
    """
    if not _owns_conversation(cid, user):
        return JSONResponse({"error": "not found"}, status_code=404)
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


@app.get("/projects")
def projects(user: dict = Depends(require_user)):
    return {"projects": list_projects(user["id"])}


@app.get("/starters")
def starters(project: str = "", user: dict = Depends(require_user)):
    project = project.strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    try:
        from backend.generate.suggest import generate_starters
        return {"questions": generate_starters(project, user["id"])}
    except Exception:  # noqa: BLE001
        log.exception("starters failed")
        return {"questions": []}


# --- POST endpoints --------------------------------------------------------
@app.post("/new")
def new_conversation(body: NewBody, user: dict = Depends(require_user)):
    return {"conversation_id": _new_conversation(user, body.project or None)}


@app.post("/delete")
def delete_conv(body: DeleteBody, user: dict = Depends(require_user)):
    if isinstance(body.conversation_id, int):
        _delete(body.conversation_id, user)
    return {"ok": True}


@app.post("/delete_brd")
def delete_brd(body: DeleteBrdBody, user: dict = Depends(require_user)):
    project = (body.project or "").strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    removed = delete_project(project, user["id"])
    try:
        from backend.generate.suggest import invalidate
        invalidate(project, user["id"])
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "removed": removed}


@app.post("/rename_brd")
def rename_brd(body: RenameBrdBody, user: dict = Depends(require_user)):
    project = (body.project or "").strip()
    title = (body.title or "").strip()
    if not project or not title:
        return JSONResponse({"error": "missing project or title"}, status_code=400)
    updated = rename_project(project, title, user["id"])
    return {"ok": True, "title": title, "updated": updated}


# --- Change a requirement (QA): edit a requirement's text, re-embed + re-index ---
@app.get("/brd/requirements")
def brd_requirements(project: str = "", user: dict = Depends(require_user)):
    project = (project or "").strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    return {"requirements": list_requirements(user["id"], project)}


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
    return {"ok": True, **res}


@app.post("/brd/requirement/structure")
def brd_requirement_structure(body: RequirementStructureBody, user: dict = Depends(require_user)):
    """Propose row boundaries for a flattened table chunk (no persistence).

    Returns {rows, flattened} — `rows` are literal substrings of the chunk that
    reconstruct it exactly (the model only chooses boundaries; see
    ingest.structure), so the user can review/edit them before splitting."""
    if not isinstance(body.chunk_id, int):
        return JSONResponse({"error": "missing chunk_id"}, status_code=400)
    from backend.db import pool
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """select c.text from brd_chunk c join brd_document d on d.id = c.document_id
               where c.id = %s and d.owner_id = %s""",
            (body.chunk_id, user["id"]),
        )
        row = cur.fetchone()
    if not row:
        return JSONResponse({"error": "requirement not found"}, status_code=404)
    text = row[0] or ""
    try:
        proposal = req_structure.propose_rows(text)
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
    except Exception as e:  # noqa: BLE001
        log.exception("requirement split failed")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return {"ok": True, **res}


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
        plan = change_agent.plan_change(user["id"], project, story)
    except VoyageUnavailable as e:
        return JSONResponse({"error": f"Search is rate-limited right now: {e}"}, status_code=503)
    except Exception as e:  # noqa: BLE001
        log.exception("requirement plan failed")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    if plan.get("error"):
        return JSONResponse({"error": plan["error"]}, status_code=400)
    return {"ok": True, **plan}


@app.post("/brd/requirement/apply")
def brd_requirement_apply(body: RequirementApplyBody, user: dict = Depends(require_user)):
    """Apply the reviewed operations from a story-driven plan through the safe pipeline."""
    project = (body.project or "").strip()
    ops = body.operations or []
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
    if not ops:
        return JSONResponse({"error": "no changes to apply"}, status_code=400)
    try:
        res = change_agent.apply_operations(user["id"], project, ops, changed_by=user["id"])
    except VoyageUnavailable as e:
        return JSONResponse({"error": f"Re-embedding is rate-limited right now: {e}"}, status_code=503)
    except Exception as e:  # noqa: BLE001
        log.exception("requirement apply failed")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return {"ok": True, **res}


@app.get("/brd/requirement/history")
def brd_requirement_history(chunk_id: int, user: dict = Depends(require_user)):
    return {"history": requirement_history(user["id"], chunk_id)}


# --- versioning + approval (draft edit → review → live) ---------------------
@app.get("/brd/versions")
def brd_versions(project: str = "", user: dict = Depends(require_user)):
    project = (project or "").strip()
    if not project:
        return JSONResponse({"error": "missing project"}, status_code=400)
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


@app.post("/upload")
async def upload(request: Request, user: dict = Depends(require_user)):
    filename = unquote(request.headers.get("X-Filename", "")).strip()
    title = unquote(request.headers.get("X-Title", "")).strip() or None
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".pdf", ".docx"):
        return JSONResponse({"error": "Only .pdf or .docx files are supported"}, status_code=400)
    data = await request.body()
    if not data:
        return JSONResponse({"error": "empty upload"}, status_code=400)
    if len(data) > MAX_UPLOAD_BYTES:
        return JSONResponse({"error": f"file too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"}, status_code=413)

    from backend.brd_pipeline.preprocessing import derive_title
    from backend.brd_pipeline.retriever_sink import (
        create_processing_document, set_document_status, slugify_project,
    )
    resolved_title = derive_title(filename, override=title)
    project = slugify_project(resolved_title)
    owner_id = user["id"]

    # Register the BRD as 'processing' up front, then embed in the background so the
    # request returns immediately (slow free-tier embedding no longer blocks the upload).
    try:
        doc_id = create_processing_document(owner_id=owner_id, title=resolved_title, project=project)
    except Exception as e:  # noqa: BLE001
        log.exception("failed to register processing BRD")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)

    def worker() -> None:
        try:
            from backend.brd_pipeline.pipeline import run_ingest_into
            n = run_ingest_into(doc_id, BytesIO(data), filename=filename)
            try:
                from backend.generate.suggest import invalidate
                invalidate(project, owner_id)
            except Exception:  # noqa: BLE001
                pass
            log.info("background ingest ready: user=%s project=%s (%d chunks)", owner_id, project, n)
        except Exception:  # noqa: BLE001
            log.exception("background ingest failed for project=%s", project)
            try:
                set_document_status(doc_id, "failed")
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "project": project, "title": resolved_title, "status": "processing"}


@app.post("/ask")
def ask(body: AskBody, user: dict = Depends(require_user)):
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
def ask_cancel(body: DeleteBody, user: dict = Depends(require_user)):
    """Stop an in-flight answer for a conversation — the worker bails out and the
    turn is NOT persisted, so a canceled answer never reappears on reload."""
    cid = body.conversation_id
    if not isinstance(cid, int) or not _owns_conversation(cid, user):
        return JSONResponse({"error": "not found"}, status_code=404)
    with _lock:
        live = _live.get(cid)
    if live is not None:
        live.cancel()
    return {"ok": True}


def main() -> None:
    import uvicorn
    host = os.getenv("BRD_HOST", "127.0.0.1")
    port = int(os.getenv("BRD_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()

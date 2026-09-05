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
from backend import email_outbox, otp
from backend.config import settings
from backend.email_send import app_base_url, build_code, build_reset, build_verification
from backend.generate.history_store import (
    conversation_user_id, create_conversation, delete_conversation, delete_project,
    get_conversation, get_messages, list_conversations, list_projects, rename_project,
    set_conversation_project,
)
from backend.generate.session import ChatSession
from backend.voyage import VoyageUnavailable

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

# --- session cache (keyed by conversation id; each conversation belongs to one user) ---
_sessions: dict[int, ChatSession] = {}
_lock = threading.Lock()


def _session_for(cid: int, user: dict) -> ChatSession:
    with _lock:
        s = _sessions.get(cid)
        if s is None:
            conv = get_conversation(cid)
            project = conv["project_scope"] if conv else None
            s = ChatSession(project=project, persist=True, user_id=str(user["id"]), owner_id=user["id"])
            s.conversation_id = cid
            s.history = get_messages(cid)
            _sessions[cid] = s
        return s


def _new_conversation(user: dict, project: str | None = None) -> int:
    cid = create_conversation(user_id=str(user["id"]), project_scope=project)
    with _lock:
        s = ChatSession(project=project, persist=True, user_id=str(user["id"]), owner_id=user["id"])
        s.conversation_id = cid
        _sessions[cid] = s
    return cid


def _delete(cid: int, user: dict) -> int:
    with _lock:
        _sessions.pop(cid, None)
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


class NewBody(BaseModel):
    project: str | None = None


class DeleteBody(BaseModel):
    conversation_id: int | None = None


class DeleteBrdBody(BaseModel):
    project: str = ""


class RenameBrdBody(BaseModel):
    project: str = ""
    title: str = ""


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
def conversations(user: dict = Depends(require_user)):
    return {"conversations": list_conversations(str(user["id"]))}


@app.get("/conversation")
def conversation(id: int | None = None, user: dict = Depends(require_user)):
    if id is None:
        return JSONResponse({"error": "missing/invalid id"}, status_code=400)
    if not _owns_conversation(id, user):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"messages": get_messages(id)}


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

    sess = _session_for(cid, user)
    req_project = body.project or None
    if sess.project is None and req_project:
        set_conversation_project(cid, req_project)
        sess.project = req_project

    q: queue.Queue = queue.Queue()
    DONE = object()

    def run() -> None:
        try:
            res = sess.ask(question, on_token=lambda t: q.put(("t", t)))
            q.put(("done", res))
        except VoyageUnavailable as e:
            q.put(("error", str(e)))
        except Exception as e:  # noqa: BLE001
            log.exception("ask failed")
            q.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            q.put((DONE, None))

    threading.Thread(target=run, daemon=True).start()

    def gen():
        while True:
            kind, payload = q.get()
            if kind is DONE:
                break
            if kind == "t":
                yield json.dumps({"t": payload}) + "\n"
            elif kind == "done":
                res = payload
                yield json.dumps({
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
                }) + "\n"
            elif kind == "error":
                yield json.dumps({"error": payload}) + "\n"

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def main() -> None:
    import uvicorn
    host = os.getenv("BRD_HOST", "127.0.0.1")
    port = int(os.getenv("BRD_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()

# BRD Retrieval Engine — HTTP API

Reference for integrating an external service with the production API. The service
indexes Business Requirements Documents (BRDs) and answers questions with grounded,
cited answers. Answers are isolated per **BRD** (`project`) and per **user account**.

- **Base URL (production):** `https://brd-engine.onrender.com`
- **Content type:** JSON request bodies (`Content-Type: application/json`); responses
  are JSON, except the answer endpoints which stream **NDJSON**.
- **Encoding:** UTF-8 throughout (answers may be Arabic or English).

---

## 1. Authentication

> **Important for external integrators.** The API has **no API-key / bearer-token
> auth**. Access is by a **session cookie** (`brd_session`, HttpOnly, `SameSite=Lax`,
> `Secure` in production) obtained through the email-based login flow. All data
> endpoints require it — requests without a valid cookie get **401**. A machine
> client must therefore keep a cookie jar and complete the login (incl. the periodic
> email OTP, below). If you need clean machine-to-machine auth, see
> [§7 Integration notes](#7-integration-notes).

### Login flow
1. `POST /auth/login` with `{ "email", "password" }`.
   - **Success:** sets the `brd_session` cookie and returns the user
     `{ "id", "email", "email_verified" }`.
   - **OTP required** (every 10th login, or an unverified account): returns
     `{ "stage": "otp", "kind": "login"|"signup", "email" }` and **no cookie yet**.
     Then `POST /auth/otp/verify` with `{ "email", "code", "kind" }` to get the cookie.
2. Send the `brd_session` cookie on every subsequent request.

`GET /auth/me` → the current user `{ id, email, email_verified }`, or **401** if the
cookie is missing/expired (use it to check session validity).

`POST /auth/logout` clears the session.

Other auth endpoints (signup, forgot/reset, change-password) exist for the app UI and
are summarized in [§6](#6-account--auth-endpoints).

---

## 2. Ask a question (primary endpoint)

### `POST /ask` → NDJSON stream

The main endpoint an external consumer calls to get an answer.

**Request body**
```json
{
  "question": "What are the reporting requirements?",
  "conversation_id": 123,        // optional; omit/null to start a new conversation
  "project": "directives",       // BRD to scope the answer to (required for a new conversation)
  "model": "anthropic/claude-opus-5"  // optional; BYOK model override, else the system default
}
```
- `question` — required, max **2000** chars.
- `conversation_id` — omit to start a new conversation (its id is returned in the
  final event). Must belong to the caller, else **404**.
- `project` — the BRD slug (see `GET /projects`). Sets the scope on a new conversation.
- `model` — optional; only used if the account has a BYOK provider key configured.

**Response:** `Content-Type: application/x-ndjson; charset=utf-8` — one JSON object per
line, read incrementally:

| Line shape | Meaning |
|---|---|
| `{"t": "<text>"}` | A token chunk of the answer (many, in order). Concatenate the `t` values to build the answer as it streams. |
| `{"done": true, ...}` | Terminal success event (full shape below). |
| `{"canceled": true}` | The turn was canceled; nothing was persisted. |
| `{"error": "<message>"}` | Generation failed; message is user-safe. |

**Final `done` event**
```json
{
  "done": true,
  "conversation_id": 123,
  "answer": "The system must ... [1][2]",
  "standalone": "the condensed standalone form of the question",
  "sources": [
    {
      "n": 1,                       // citation number referenced as [1] in answer
      "req_id": "FR-12",            // requirement id (may be null)
      "section": "3.2 Reporting",   // section heading (may be null)
      "project": "directives",
      "snippet": "short preview of the cited requirement text",
      "full": "the full cited requirement text"
    }
  ]
}
```
Consumers that only need the answer can wait for the `done` event and read `answer`
+ `sources`; consumers that want live output render the `t` chunks as they arrive.

**Synchronous errors** (returned as normal JSON with a 4xx status, not in the stream):
- `400` `{"error":"empty question"}`
- `413` `{"error":"question too long (max 2000 chars)"}`
- `404` `{"error":"not found"}` — `conversation_id` not owned by the caller
- `409` `{"error":"Still answering your previous question — one moment."}` — one active
  turn per conversation; retry after the current one finishes or call `/ask/cancel`.

### `POST /ask/cancel`
Body `{ "conversation_id" }`. Stops an in-flight answer; the canceled turn is **not**
persisted.

### `GET /conversation/{cid}/stream`
Re-attach to an answer still generating for a conversation (e.g. after a reconnect).
Emits `{"question": "..."}` first, then the same token/terminal events as `/ask`; emits
`{"idle": true}` if nothing is generating.

---

## 3. Discover BRDs & conversations (read)

| Method & path | Returns |
|---|---|
| `GET /projects` | `{ "projects": [ { … } ] }` — the BRDs (projects) available to the user; use a project slug as `project` in `/ask`. |
| `GET /starters?project=<slug>` | `{ "questions": [ "…", … ] }` — suggested starter questions for a BRD. |
| `GET /conversations?limit=30&before=<id>` | `{ "conversations": [ … ], "has_more": bool }` — the user's conversations, newest first (keyset paginate with `before`). |
| `GET /conversation/{cid}` | `{ "messages": [ … ] }` — full message history of one conversation (404 if not owned). |
| `GET /health` | `{ "status": "ok"\|"degraded", "db": bool }` — liveness + DB check (no auth). |

### Conversation management
- `POST /new` — body `{ "project"? }` → creates a conversation, returns its id.
- `POST /delete` — body `{ "conversation_id" }` → deletes a conversation.

---

## 4. Read BRD requirements (read)

An external service that needs the structured requirements (not just answers):

- `GET /brd/requirements?project=<slug>` → `{ "requirements": [ … ] }` — the requirements
  (chunks) of a BRD in document order, each independently addressable by `chunk_id`.
- `GET /brd/versions?project=<slug>` → the BRD's version snapshots.
- `GET /brd/requirement/history?chunk_id=<id>` → the edit history of one requirement.

Write/edit endpoints (`/brd/requirement/{update,add,delete,revert,structure,split,plan,
apply,scope_edit}`, `/brd/version/{snapshot,restore}`, `/brd/{approve,discard}`,
`/upload`, `/rename_brd`, `/delete_brd`) exist for the authoring UI. They mutate BRD
content and are **not** part of a read-only consumer integration; document them
separately before granting an external service write access.

---

## 5. Errors & limits

- **Error shape:** `{"error": "<message>"}` with an appropriate HTTP status. Messages
  are safe to surface to end users.
- **Status codes:** `400` bad input · `401` not authenticated · `404` not found / not
  owned · `409` conversation busy · `413` payload too large · `422` bad path param ·
  `429` rate-limited · `503` a dependency (search embeddings) is throttled.
- **Rate limits:** auth endpoints (`signup`, `login`, `otp/verify`, `otp/resend`,
  `forgot`, `resend-verification`) are IP/user rate-limited and return `429` with a
  `Retry-After` header. Upstream free-tier limits (Voyage embeddings, free LLM models)
  can surface as transient `503`/`error` events — retry with backoff.

---

## 6. Account & auth endpoints (summary)

| Method & path | Body | Notes |
|---|---|---|
| `POST /auth/signup` | `{ email, password }` | Creates the account, emails a `signup` code, returns `{ stage:"otp", … }`. No session until verified. |
| `POST /auth/otp/verify` | `{ email, code, kind }` | Verifies a `signup`/`login` code; sets the session cookie. |
| `POST /auth/otp/resend` | `{ email, kind }` | Re-sends a code (rate-limited). |
| `POST /auth/login` | `{ email, password }` | Session, or `{ stage:"otp" }` (see §1). |
| `POST /auth/logout` | — | Clears the session. |
| `GET /auth/me` | — | Current user, or 401. |
| `POST /auth/forgot` | `{ email }` | Emails a `reset` code; always returns `{ stage:"otp", kind:"reset" }` (never reveals if the email exists). |
| `POST /auth/reset` | `{ email, code, password }` | Sets a new password; signs in. |
| `POST /auth/change-password` | `{ current_password, new_password }` | Session-authed. |

BYOK provider-key management (`/llm/*`) is also session-authed; see the code if the
external service needs to drive per-user model selection.

---

## 7. Integration notes

- **No bearer/API-key auth today.** External automation must log in and carry the
  `brd_session` cookie, and handle the periodic **email OTP** on login. For a clean
  server-to-server integration, consider adding a token/service-account mechanism
  before onboarding an external consumer — the current model is designed for browser
  users.
- **Isolation is per user + per project.** A session only sees its own conversations
  and BRDs; answers are scoped to the `project` you pass. There is no cross-tenant read.
- **Streaming:** consume `/ask` as a line-delimited (NDJSON) stream — don't wait for the
  whole body. `Cache-Control: no-cache` and `X-Accel-Buffering: no` are set so proxies
  don't buffer. If you only need the final answer, read until the `done` line.
- **One active turn per conversation** (`409` otherwise). Use separate conversations for
  parallelism.
- **CORS:** the API primarily serves its own SPA (same-origin). A browser-based external
  client on another origin needs CORS to be enabled server-side first — confirm before
  building against it from a different domain.
- **Idempotency:** `/ask` is not idempotent (each call is a new turn). Cancel via
  `/ask/cancel` rather than dropping the connection if you need to abort cleanly.

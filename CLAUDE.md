# BRD Retrieval Engine

A RAG system that indexes Business Requirements Documents (BRDs) as vectors and answers
questions with grounded, cited answers. Multiple BRDs coexist in one database, isolated
per `project`. This file is both the working discipline for the project and a handoff recap.

---

# Context & Token Discipline

These rules exist to keep the session lean. Do NOT overuse tools or over-read the codebase.

## Before reading anything
- **Ask before exploring.** If the task can be answered from what's already in context or from a single targeted read, do that. Do not "get oriented" by scanning the repo.
- **No speculative reading.** Only open a file when you have a concrete reason tied to the current task. "Might be relevant" is not a reason.
- **Trust what I tell you.** If I name a file, function, or line, go straight there. Don't verify surrounding structure unless the change requires it.

## Reading files
- Read the **minimum span** needed. Prefer targeted ranges over whole files. Only read a whole file when it's small or you genuinely need all of it.
- **Never re-read a file already in context.** If you've seen it this session and it hasn't changed, use what you have.
- Don't read a file "to confirm" something you can reason about. Confirm only when the answer materially changes the edit.
- When searching, use one precise `grep`/glob with a specific pattern instead of several broad ones. Don't chain exploratory searches.

## Tool use
- **One tool call should do one job.** Don't fan out into parallel reads across the tree "to be safe."
- **No subagents / Task delegation** for simple work. Do it inline in the main thread.
- Don't run a command to gather information you can already infer or that I've already given you.
- Batch related edits to the same file rather than re-opening it per change.
- Stop calling tools once you have enough to act. Don't keep gathering context past the point of sufficiency.

## Codebase scope
- Stay within the files directly relevant to the task. Do not trace every import, caller, or dependency unless the change actually requires them.
- Don't build a full mental map of the project unless I explicitly ask for an architecture overview.
- If you think you need broad context to proceed, **stop and ask me** which files matter instead of reading widely.

## When unsure
- If a task seems to need extensive exploration, ask me to point you at the right files rather than searching. A short question is cheaper than a wide scan.
- Prefer asking one clarifying question over reading ten files to guess the answer.

## Output
- Keep responses focused. Don't restate large chunks of files back to me.
- Reference code by path and line, don't paste it back unless I ask.

---

# Session Handoff — Project State

## What it is
Ingest BRDs → retrieve the relevant chunks → answer with an LLM, grounded in the retrieved
text and citing the requirement each claim came from. Built phase by phase (0→3) plus
hardening, a chat UI, and multi-BRD isolation.

## Stack
| Concern | Choice |
|---|---|
| Language | Python 3.14 (venv at `.venv`) |
| Storage + vectors | PostgreSQL 16 + **pgvector** (HNSW), in Docker (`brd_pg`, host port **5433**) |
| Embeddings / rerank | **Voyage AI** — `voyage-4` (docs) / `voyage-4-lite` (queries) / `rerank-2.5` |
| Generation | **OpenRouter** via the `openai` SDK; `GEN_MODEL` selects the model |
| Connections | `psycopg_pool` connection pool (`backend/db.py`, lazy, atexit-closed) |
| Backend API | `backend/api.py` — **FastAPI** + uvicorn (JSON endpoints + serves the built SPA) |
| Frontend | **React + Vite + TypeScript** in `frontend/` (built to `frontend/dist`) |

## Config (.env — single source of truth, no hardcoded defaults)
- `DATABASE_URL` → `postgresql://brd:brd_local_pw@localhost:5433/brd_real`
- `VOYAGE_API_KEY`, `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`
- `GEN_MODEL` (currently a free model, e.g. `minimax/minimax-m3:free`; switch to `anthropic/claude-opus-5` once OpenRouter has credit)
- `REFINE_VERIFY=1` (gated LLM answer-verification), `VOYAGE_EMBED_BATCH=10`
- `CACHE_ENABLED=1` (Postgres-backed cache; see Caching below), `CACHE_TTL_SECONDS=0` (0 = no age limit; busting on ingest is the primary invalidation)
- `APP_BASE_URL`, `SMTP_HOST`/`SMTP_PORT`/`SMTP_SECURITY` (`starttls`|`ssl`|`none`)/`SMTP_USER`/`SMTP_PASSWORD`/`SMTP_FROM`/`SMTP_FROM_NAME` — auth email. Unset `SMTP_HOST` = dev mode (link logged to console, nothing sent).
- `OTP_TTL_MINUTES` (10), `OTP_MAX_ATTEMPTS` (5), `OTP_LOGIN_EVERY` (10) — emailed 6-digit codes (see Auth / OTP below).
- `LLM_KEY_SECRET` — Fernet key encrypting users' BYOK provider keys (see Custom LLM below). Unset = BYOK off.
- Print resolved config (secrets masked): `.venv\Scripts\python.exe -m backend.config`

## Databases (same Docker server)
- **`brd`** — demo/test DB: payments + reporting synthetic samples (used by golden set / eval).
- **`brd_real`** — real data, project-scoped: `directives` (214) + `es` (6) + `aoun-sub` (5). `.env` points here.

## Layout
- `backend/ingest/` — `parser.py` (heading-aware; also captures heading-less docs), `chunker.py` (section-aware + content-hash idempotency), `embedder.py`, `store.py` (2-tx upsert, embed outside the transaction), `pipeline.py`, `docx_to_md.py` (Word→Markdown), `pdf_to_md.py` (PDF→Markdown via pypdf). Chunker has a word-window fallback so a heading/paragraph-less page never yields an oversized chunk.
- `backend/retrieve/` — `search.py` (vector), `keyword.py` (FTS over `search_tsv`), `rerank.py` (Voyage), `retriever.py` (vector + keyword → RRF → rerank, project/version/status filters).
- `backend/generate/` — `llm.py` (OpenRouter client, 429 retry, `message_text`), `answer.py` (grounded+cited prompt, `parse_citations`), `condense.py` (follow-up → standalone), `session.py` (`ChatSession`: front-refine → condense → retrieve → answer → grounding guard → verify), `history_store.py`, `refine.py` (small-talk routing + grounding guard + gated verifier).
- `backend/db.py` — pool + `python -m backend.db` health check. `backend/config.py` — settings. `backend/cache.py` — fail-open Postgres cache (see Caching below).
- `backend/email_send.py` — SMTP transport + branded templates. `send_email() -> bool`, never raises; transports `starttls`/`ssl`/`none`; dev mode logs the link when `SMTP_HOST` unset. `render_action_email` / `build_verification` / `build_reset` produce (subject, plain, HTML). `signup`/`forgot`/`resend-verification` are rate-limited.
- `backend/email_outbox.py` — durable send with FIXED-interval retry (see Email outbox below). `enqueue()` writes a row + fires an immediate send; a startup sweeper retries failures every `EMAIL_RETRY_INTERVAL_SECONDS` (default 180s, not exponential) up to `EMAIL_MAX_ATTEMPTS`, then dead-letters. Scoped per user/recipient; race-free atomic claim.
- `backend/otp.py` — emailed 6-digit codes (see Auth / OTP below). `issue`/`verify` (argon2-hashed, expiring, attempt-capped) + `login_needs_otp` (every Nth login).
- `backend/crypto.py` — Fernet encrypt/decrypt for secrets at rest. `backend/llm_keys.py` — per-user BYOK provider keys (validate/store/decrypt/list-models); see Custom LLM below.
- `migrations/` — `001_init.sql` (schema + HNSW), `002_search_tsv.sql` (generated `search_tsv` GIN index, `simple` config for Arabic + exact tokens), `007_cache.sql` (`cache_kv`), `008_email_outbox.sql` (`email_outbox`), `009_otp.sql` (`otp_code` + `app_user.login_count`), `010_llm_keys.sql` (`user_llm_key` + `conversation.model`), `011_llm_preferred_models.sql` (`user_llm_key.preferred_models` jsonb). 008/009/010/011 are brd_real only (FK to `app_user`). (003–006 are auth + progress.)
- `tests/` — 78 pytest unit tests (pure logic, no network/DB); `test_cache.py`, `test_email.py`, `test_email_outbox.py`, `test_otp.py`, `test_byok.py` (per-key client caching/routing, key validate/mask, preferred-model dedup/intersect).
- `backend/api.py` — FastAPI app (streaming `/ask`, `/upload`, `/projects`, `/starters`, `/rename_brd`, `/delete_brd`, `/health`, …); serves `frontend/dist`.
- `frontend/` — React+Vite+TS SPA (`src/App.tsx`, `src/components/*`, `src/lib/*`). `data/brds/*.md`, `data/golden_qa.json`.

## Run
```bash
docker compose up -d                              # Postgres + pgvector (port 5433)
.venv\Scripts\python.exe -m backend.db                # Phase-0 health check
.venv\Scripts\python.exe -m backend.ingest.pipeline data/brds/<file>.md
.venv\Scripts\python.exe -m backend.retrieve.retriever "question" --project <es|directives>
.venv\Scripts\python.exe -m pytest -q             # 78 tests
cd frontend && npm install && npm run build && cd ..       # build the React SPA (once)
.venv\Scripts\python.exe -m backend.api                    # FastAPI at http://localhost:8000
```
Add a new BRD safely: `docx_to_md.py --in "<path>" --project <name> ...` → `pipeline` → it's a new `project`; the UI's BRD selector scopes each chat to one.

## UI features (frontend/ React app)
Multi-conversation sidebar (per-browser `sid` cookie) with new/switch/delete; **BRD selector** (each chat pinned to one `project`, scope pill in header); **streaming** answers (NDJSON token stream); safe **Markdown rendering** (headings/tables/lists, escape-first XSS-safe); **citation hover tooltips** + sources list (also on reloaded sessions, reconstructed from `cited_chunk_ids`); dark/light toggle (dark default); playful loading lines; Arabic/English via `dir="auto"`.

## Caching (`backend/cache.py`, `cache_kv` table)
Memoizes the three paid / rate-limited stages of a question so repeats are near-instant and spend no Voyage/OpenRouter budget. One `cache_kv(key, namespace, owner_id, project, value, created_at)` table, three namespaces:
- **`embed`** `(text, model) → query vector` — **immutable**, never expires, never busted (a query embedding is a pure function of its inputs). Hooked in `embedder.embed_query`. Biggest defence against the free-tier RPM cap.
- **`retrieve`** `(query, owner_id, project, k_final, candidates, use_rerank) → ranked chunks` — a hit skips embed **and** rerank. Hooked in `retriever.retrieve` (get before any pool checkout, set after). Live-measured ~673× faster on a hit.
- **`answer`** `(standalone, ordered chunk_ids, GEN_MODEL) → final answer` — a hit replays the stored (post-verify) answer through `on_token` and reuses `_finish`, skipping generation entirely. Hooked in `ChatSession.ask`. Live-measured ~2273× faster; never caches a transient-failure fallback. History is **not** in the key (condense already folds it into the standalone).

Three invariants (enforced in `cache.py`): **fail-open** (any error → miss, never raises into a request); **no nested pool checkouts** (get/set are short-lived, called outside `with pool()` blocks — the pool is `max_size=10`); **model id in every key** (so a `GEN_MODEL` / embed-model swap self-invalidates). Invalidation of corpus-dependent namespaces is by `cache.bust_project(project)` on ingest (`store.upsert`, only when chunks changed) and delete (`history_store.delete_project`); `embed` is left intact. TTL (`CACHE_TTL_SECONDS`) is only a safety net; `prune_expired()` sweeps old retrieve/answer rows. `007_cache.sql` is applied to **both** `brd_real` and the `brd` demo DB.

## Email & outbox (`backend/email_send.py`, `backend/email_outbox.py`, `email_outbox` table)
Auth email (verification + password reset) is **durable**: `enqueue()` writes an `email_outbox` row and fires an immediate send in a daemon thread; a startup **sweeper** retries failures. Scope is **per user/recipient** (authentication is per-user — no session exists yet at signup/reset).
- **Retry** is a **FIXED interval** (`EMAIL_RETRY_INTERVAL_SECONDS`, default 180s — deliberately not exponential, which is poor UX for auth mail) up to `EMAIL_MAX_ATTEMPTS`, then the row dead-letters (`status='failed'`).
- **Three limits:** request limit (`max_attempts`, `attempts++` at claim time so a crash still counts); per-attempt countdown (`next_attempt_at = now()+interval`); post-exhaustion **cooldown** (`EMAIL_RECIPIENT_COOLDOWN_SECONDS`) — a dead-letter suppresses new mail to that recipient.
- **Race-free:** a row is claimed with one atomic `UPDATE … status='sending'` guarded on current status; the sweeper uses `FOR UPDATE SKIP LOCKED`, so the immediate attempt and any number of workers never double-send. A lease (`locked_at`, `EMAIL_LEASE_SECONDS`) reclaims a crashed worker's row; `reclaim_stuck()` runs at startup. Delivery is **at-least-once** (a send that succeeds but crashes pre-commit may repeat — harmless here; exactly-once would need a provider idempotency key).
- **Transport:** `SMTP_SECURITY` = `starttls` (587) | `ssl` (465) | `none` (dev relay). No `SMTP_HOST` ⇒ dev mode logs the link. HTML emails via `render_action_email` (inline-CSS, escaped). `008_email_outbox.sql` is **brd_real only** (FK to `app_user`).
- **Auth of the sender mailbox:** currently a Gmail **App Password** (`SMTP_USER`/`SMTP_PASSWORD`). **Planned:** OAuth2 (Google refresh-token, XOAUTH2) sender so no static password is stored — swap only lives in `email_send.py` `_connect()`/login; recipients + outbox unaffected.

## Auth / OTP (`backend/otp.py`, `otp_code` table)
Authentication uses **emailed 6-digit codes** (2-step), delivered via the durable outbox. Codes are argon2-hashed at rest, expire (`OTP_TTL_MINUTES`), and are attempt-capped (`OTP_MAX_ATTEMPTS`); a new code supersedes the prior one.
- **Signup** = blocking gate: `/auth/signup` creates the user but issues **no session** — it emails a `signup` code and returns `{stage:"otp"}`; `/auth/otp/verify` checks the code, marks verified, and only then sets the session cookie.
- **Login**: password-only most times; **every `OTP_LOGIN_EVERY`-th login** (`app_user.login_count`) emails a `login` code and returns `{stage:"otp"}` before a session is granted. An unverified account logging in is routed back to signup verification.
- **Forgot/reset**: `/auth/forgot` emails a `reset` code (always returns `{stage:"otp"}`, never revealing whether the email exists); `/auth/reset` takes `{email, code, password}`.
- Endpoints `/auth/otp/verify` and `/auth/otp/resend` are rate-limited. The session cookie is the gate — because it's only issued post-verification, protected routes need no extra `verified` check.
- **Change password**: `/auth/change-password` (session-authed) verifies the current password then sets the new one. UI: an **Account Settings** view (`AccountSettings.tsx`) that replaces the conversation area — opened by clicking the email in the sidebar footer, closed via "← Back to chat" in the topbar.
- **Frontend**: `AuthPage.tsx` has a code-entry stage for all three flows. The old post-login "verify your email" **link banner is hidden** behind `SHOW_VERIFY_BANNER=false` in `App.tsx` (kept for a future link mode); the link endpoints (`/auth/verify`, `_send_verification`) and `build_verification`/`build_reset` templates are also kept but unused. The obsolete `ResetPasswordPage.tsx` (link reset) was removed — reset is now code-based in `AuthPage`.

## Custom LLM — bring-your-own-key (v1, OpenAI-compatible only)
A user can supply their **own** provider API key (OpenAI-compatible: `base_url` + key) and pick the generation model per chat.
- **Storage:** `user_llm_key` (one per user) — key **encrypted at rest** via Fernet (`backend/crypto.py`, `LLM_KEY_SECRET`); only a masked hint (`sk-…abcd`) is ever returned. Endpoints: `GET/POST/DELETE /llm/key`, `GET /llm/models` (cached in the `llm_models` cache namespace).
- **Model curation:** after a key validates, a **"choose your models" modal** (`ModelsModal.tsx`) lists every model the key can reach; the user ticks a subset stored in `user_llm_key.preferred_models` (`GET /llm/models` returns `{models, preferred}`; `POST /llm/preferred` writes it, deduped + intersected with what the key can serve). Search in the modal is **hidden behind a search icon** (bar appears on click, filters live). Re-openable from the in-chat picker's "Manage models…" and from Account Settings' "Choose models".
- **Model picker:** a Claude-Desktop-style pill+popover (`ModelPicker.tsx`) shows the **preferred** subset (falls back to all models until curated). The selected model is stored on `conversation.model` and sent per `/ask` (`AskBody.model`); shown when a key is configured.
- **Generation routing:** `llm.chat()` / `client()` take optional `api_key`+`base_url`, memoized per key (`_byok_clients`). `ChatSession` carries `gen_key`/`gen_base_url`/`gen_model`, threaded through **condense + answer + verify**. `/ask` resolves the route per request (key + chosen model), so changes take effect immediately; **falls back to the system `GEN_MODEL`/OpenRouter** when no key/model. Answer-cache key uses the **effective** model, not the global `GEN_MODEL`.
- **Validation:** saving a key lists the provider's models (proves it works) before storing; `InvalidKey` → clean 400. **Anthropic and other non-OpenAI-shaped APIs are out of scope for v1** (would need per-provider adapters).

## Known constraints
- **Free-tier limits:** Voyage without a payment method = 3 req/min + 10K tokens/min (throttled in `backend/voyage.py`); free OpenRouter models 429 under load (retry/fallback in place). Add credit + a capable Claude model for smooth, reliable behavior.
- The UI is a React (Vite) SPA in `frontend/`, served by FastAPI (`backend/api.py`) from `frontend/dist`. The earlier stdlib `serve.py` and Chainlit `app.py` were removed.
- **Isolation is logical** (SQL `project` filter), not physical; a strict deployment would use RLS or separate DBs.
- Testing PowerShell can't read the no-`Content-Length` stream — use `curl --data @file` to test `/ask`.

## Scale path (from the blueprint)
Postgres + pgvector now → add **pgvectorscale** (stay in Postgres) when HNSW strains → move the vector index to **Qdrant** only when a dedicated, horizontally-scaled service is needed. Keep Postgres as source of truth.

# BRD Retrieval Engine

A RAG system that indexes Business Requirements Documents (BRDs) as vectors and
answers questions with cited sources. Phase 1 is a grounded chatbot; it scales
later to generating test cases and tickets from the same retrieval core.

**Architecture blueprint:** https://claude.ai/code/artifact/9d8d3457-b659-49b1-84aa-0fd7bb140b11

## Stack

| Layer       | Choice                                              |
| ----------- | --------------------------------------------------- |
| Embeddings  | Voyage `voyage-4` (docs) / `voyage-4-lite` (queries)|
| Rerank      | Voyage `rerank-2.5`                                 |
| Store       | Postgres 16 + pgvector (HNSW)                       |
| Generation  | Claude `claude-opus-5` (native citations)           |
| Backend     | Python 3.12+ · **FastAPI** + uvicorn (`backend/api.py`)         |
| Frontend    | **React + Vite + TypeScript** (`frontend/`, built to `frontend/dist`) |

## Phase 0 — stand up the storage layer

Everything downstream depends on the database, and this step needs **no API keys**.

```bash
# 1. Configure (DATABASE_URL is pre-filled; API keys can stay blank for now)
cp .env.example .env

# 2. Boot Postgres + pgvector. The schema in ./migrations is applied on first boot.
docker compose up -d

# 3. Install the Python deps
pip install -e .

# 4. Run the Phase-0 exit test
python -m backend.core.db
```

Expected output:

```
pgvector .......... ok (v0.x.x)
tables ............ ok (5 present)
hnsw index ........ ok

Phase 0 exit test PASSED — the storage layer is live.
```

## Run the chat UI (server)

The backend is **FastAPI** (`backend/api.py`); the UI is a **React (Vite)** app in
`frontend/`. Chatting and uploading need the Voyage and OpenRouter keys in `.env`.

```bash
# Optional: confirm the storage layer is live first
.venv\Scripts\python.exe -m backend.core.db

# Build the React frontend once (outputs to frontend/dist)
cd frontend && npm install && npm run build && cd ..

# Start the API (serves the built SPA + JSON endpoints) at http://localhost:8000
.venv\Scripts\python.exe -m backend.api
```

**Frontend dev (hot reload):** in one terminal run `.venv\Scripts\python.exe -m backend.api`
(the API on :8000); in another run `cd frontend && npm run dev` (Vite on :5173, proxying
API calls to :8000). Edit under `frontend/src/`.

### Manage BRDs (in the UI)

The sidebar **Manage BRDs** button opens a modal to:

- **Upload** a `.pdf` / `.docx` and give it a custom **name** — stores the file in object
  storage and queues a durable background job (parse → chunk → embed → store); it joins the
  BRD selector once the job finishes. Uploading a file whose title matches an existing BRD
  stages it as a **new version** for review (Apply / Discard) instead — the live BRD keeps
  answering in the meantime.
- **Delete** a BRD — deletes it entirely in one transaction: the document, its chunks, **and
  its conversations, use cases, test results and audit log** (nothing related is kept).
  Stored source objects and cached vectors are purged afterward.

### Ingest / query from the command line

```bash
# Ingest a BRD the same way the UI upload does (object storage + a durable job)
.venv\Scripts\python.exe -m backend.ingest.pipeline data/brds/<file>.pdf --owner-id <id> --wait

# A new version of an existing BRD (staged for review, not applied automatically)
.venv\Scripts\python.exe -m backend.ingest.pipeline data/brds/<file>.docx --owner-id <id> --project <slug>

# Query a BRD (project = the slug shown in the UI selector)
.venv\Scripts\python.exe -m backend.retrieve.retriever "your question" --project <slug>
```

`backend.legacy.brd_pipeline` is the **old, structure-blind** upload path: kept only for its
own tests, not wired to `/upload` or to the CLI above, and **not for real data** — it has no
owner/version/object-storage support and predates non-destructive deletes.

## Roadmap

- **0 Foundations** — schema + docker *(this step)*
- **1 Ingest** — parse → chunk → embed → store
- **2 Retrieve** — vector search + filter + rerank
- **3 Chat** — grounded, cited, streaming answers *(the deliverable)*
- **4 Evaluate** — faithfulness + observability
- **5 Harden** — access control + scale
- **6 Scale up** — generate test cases (QAPilot) and tickets (ClickUp)

## Deployment

The app ships as a **multi-stage** container image (`Dockerfile`) — Node builds the
React SPA, then Python (FastAPI/uvicorn, `backend.api`) serves it. A
self-contained production stack lives in `docker-compose.prod.yml` — Postgres +
pgvector and the web app in one command, separate from the local dev
`docker-compose.yml`.

```bash
# Provide secrets via the shell or a (git-ignored) .env, then:
docker compose -f docker-compose.prod.yml -p brd_prod up -d --build
# App on http://localhost:8000  (front with a TLS reverse proxy for public use)
```

- **Config** is entirely env-driven (see `backend/core/config.py`); the compose file lists
  every variable. Required secrets (`POSTGRES_PASSWORD`, `VOYAGE_API_KEY`,
  `OPENROUTER_API_KEY`, `GEN_MODEL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`) fail fast if unset.
  Uploaded BRD source files are stored in a bundled MinIO service (named volume) so they
  survive a container rebuild — point `S3_*` at AWS S3 / R2 instead for a hosted bucket.
- **Host/port** are configurable via `BRD_HOST` / `BRD_PORT` (default
  `127.0.0.1:8000` locally, `0.0.0.0:8000` in the image).
- **Health probe**: `GET /health` returns `{"status":"ok","db":true,"storage":true}`
  (503 if the DB is unreachable; status/code are DB-only — `storage` is a background-
  refreshed flag so a dead object store never slows the probe itself) — used by the
  container `HEALTHCHECK` and any orchestrator.
- **Schema** in `migrations/` is applied on the DB's first boot. The DB port is
  **not** published in prod — only the web service reaches it.
- **Concurrency**: a single uvicorn worker; put a TLS reverse proxy in front, and for
  a multi-user public deployment add authentication first. The free-tier Voyage budget
  is tracked per process, so this stays one API process (see Known constraints).

### Optional extras

```bash
pip install ".[dev]"        # ruff + pytest
```

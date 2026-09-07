# syntax=docker/dockerfile:1
# BRD Retrieval Engine — multi-stage image: Node builds the React SPA, Python serves it.

# --- Stage 1: build the frontend (Vite + React) ---
FROM node:22-slim AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build            # -> /fe/dist

# --- Stage 2: Python runtime (FastAPI + uvicorn) ---
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    BRD_HOST=0.0.0.0 \
    BRD_PORT=8000

WORKDIR /app

# Python deps first for layer caching.
COPY pyproject.toml README.md ./
COPY backend ./backend
RUN pip install .

# Schema + the built SPA (served from /app/frontend/dist by backend/api.py).
COPY migrations ./migrations
COPY --from=frontend /fe/dist ./frontend/dist

# Entrypoint: run migrations (idempotent) then exec the server.
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request,sys; p=os.getenv('PORT') or os.getenv('BRD_PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health').status==200 else 1)"

CMD ["./docker-entrypoint.sh"]

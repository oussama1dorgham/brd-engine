"""Database access: a lazily-opened connection pool + the Phase-0 health check.

Every module gets connections from `pool()` instead of opening its own, so we
reuse connections across queries instead of paying a TCP + auth handshake each
time — the prerequisite for handling concurrent requests.

The pool is created on first use (not at import), so importing modules that
touch the DB does not require Postgres to be up (keeps unit tests offline).
"""
from __future__ import annotations

import atexit
import logging

import psycopg
from psycopg_pool import ConnectionPool

from .config import settings

log = logging.getLogger("brd.db")

EXPECTED_TABLES = ["brd_document", "brd_chunk", "brd_embedding", "conversation", "message"]

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    """Return the process-wide connection pool, opening it on first use."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(settings.database_url, min_size=1, max_size=10, open=True)
        log.info("opened connection pool (max_size=10)")
    return _pool


def _close_pool() -> None:
    """Close the pool at normal exit, before interpreter finalization, so its
    worker thread is joined cleanly (avoids a PythonFinalizationError on 3.14)."""
    global _pool
    if _pool is not None:
        try:
            _pool.close()
        finally:
            _pool = None


atexit.register(_close_pool)


def connect() -> psycopg.Connection:
    """A standalone connection for one-off scripts / the health check."""
    return psycopg.connect(settings.database_url)


def health_check() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector';")
        row = cur.fetchone()
        if not row:
            raise SystemExit("FAIL: pgvector extension not installed.")
        print(f"pgvector .......... ok (v{row[0]})")

        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';"
        )
        present = {r[0] for r in cur.fetchall()}
        missing = [t for t in EXPECTED_TABLES if t not in present]
        if missing:
            raise SystemExit(f"FAIL: missing tables: {', '.join(missing)}")
        print(f"tables ............ ok ({len(EXPECTED_TABLES)} present)")

        cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'brd_embedding_hnsw_idx';")
        if not cur.fetchone():
            raise SystemExit("FAIL: HNSW index 'brd_embedding_hnsw_idx' not found.")
        print("hnsw index ........ ok")

    print("\nPhase 0 exit test PASSED — the storage layer is live.")


if __name__ == "__main__":
    health_check()

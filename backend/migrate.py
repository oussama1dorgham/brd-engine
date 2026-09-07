"""Apply database migrations in order against DATABASE_URL.

Every migration in migrations/ is written idempotently (CREATE ... IF NOT EXISTS,
ADD COLUMN IF NOT EXISTS, etc.), so re-running the whole set is safe — no separate
"applied migrations" ledger is needed. Each file is applied in its own transaction,
so a file either fully applies or rolls back.

Run before serving (locally, or as a platform pre-deploy step):

    python -m backend.migrate
"""
from __future__ import annotations

import os
import pathlib
import sys

import psycopg
from dotenv import load_dotenv

load_dotenv()   # local convenience; on a platform, DATABASE_URL is a real env var

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations"


def main() -> int:
    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    files = sorted(MIGRATIONS.glob("*.sql"))
    if not files:
        print(f"no .sql migrations found in {MIGRATIONS}", file=sys.stderr)
        return 1
    with psycopg.connect(url) as conn:
        for f in files:
            print(f"applying {f.name} …", flush=True)
            with conn.transaction():
                conn.execute(f.read_text(encoding="utf-8"))
    print(f"done — {len(files)} migration file(s) applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

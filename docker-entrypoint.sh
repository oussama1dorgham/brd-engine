#!/bin/sh
# Apply DB migrations (idempotent), then hand off to the server as PID 1 so
# signals (SIGTERM on deploy/restart) reach uvicorn for a graceful shutdown.
set -e
python -m backend.migrate
exec python -m backend.api

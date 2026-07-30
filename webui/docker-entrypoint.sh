#!/bin/bash
set -euo pipefail

echo "[entrypoint] Starting web server on :3050..."
exec python3 -m uvicorn main:app \
    --host 0.0.0.0 \
    --port 3050 \
    --workers 1 \
    --log-level info

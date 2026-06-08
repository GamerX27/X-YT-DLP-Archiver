#!/bin/bash
set -euo pipefail

MODEL="${OLLAMA_MODEL:-llama3.2:1b}"

# ---------------------------------------------------------------------------
# 1. Start Ollama server in the background
# ---------------------------------------------------------------------------
echo "[entrypoint] Starting Ollama server..."
ollama serve &
OLLAMA_PID=$!

# ---------------------------------------------------------------------------
# 2. Wait until the Ollama API is accepting requests
# ---------------------------------------------------------------------------
echo "[entrypoint] Waiting for Ollama to be ready..."
MAX_WAIT=90
WAITED=0
until curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; do
    if [ "$WAITED" -ge "$MAX_WAIT" ]; then
        echo "[entrypoint] ERROR: Ollama did not start within ${MAX_WAIT}s — aborting." >&2
        kill "$OLLAMA_PID" 2>/dev/null || true
        exit 1
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done
echo "[entrypoint] Ollama ready (${WAITED}s)"

# ---------------------------------------------------------------------------
# 3. Pull the configured model if it is not already on disk
# ---------------------------------------------------------------------------
if ollama list 2>/dev/null | grep -q "^${MODEL}"; then
    echo "[entrypoint] Model '${MODEL}' already present — skipping pull"
else
    echo "[entrypoint] Pulling model '${MODEL}' (first run — may take several minutes)..."
    ollama pull "${MODEL}"
    echo "[entrypoint] Model '${MODEL}' ready"
fi

# ---------------------------------------------------------------------------
# 4. Hand off to the FastAPI web server (exec replaces the shell so tini
#    can properly manage both the uvicorn process and ollama in the bg)
# ---------------------------------------------------------------------------
echo "[entrypoint] Starting web server on :3050..."
exec python3 -m uvicorn main:app \
    --host 0.0.0.0 \
    --port 3050 \
    --workers 1 \
    --log-level info

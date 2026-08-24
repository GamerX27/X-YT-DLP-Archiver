#!/bin/bash
set -euo pipefail

# requirements.txt pins yt-dlp/yt-dlp-ejs unversioned, but that only pulls
# latest at image build time — a long-lived container would otherwise keep
# whatever was installed then. YouTube changes break extraction (403s,
# missing/low-res formats) often enough that yt-dlp ships near-daily fixes,
# so re-check for an update on every start. Best-effort: a failed upgrade
# (e.g. offline) must not block startup with whatever version is already there.
echo "[entrypoint] Checking for yt-dlp updates..."
pip install --no-cache-dir --upgrade yt-dlp yt-dlp-ejs \
    || echo "[entrypoint] yt-dlp upgrade check failed, continuing with installed version"

echo "[entrypoint] Starting web server on :3050..."
exec python3 -m uvicorn main:app \
    --host 0.0.0.0 \
    --port 3050 \
    --workers 1 \
    --log-level info

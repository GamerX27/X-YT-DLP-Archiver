#!/bin/bash
set -euo pipefail

YTDLP_BRANCH="$(echo "${YTDLP_BRANCH:-stable}" | tr '[:upper:]' '[:lower:]')"
if [ "$YTDLP_BRANCH" = "nightly" ]; then
    PIP_UPGRADE=(python3 -m pip install --no-cache-dir --upgrade --pre yt-dlp yt-dlp-ejs)
    UPDATE_INTERVAL_SECONDS=86400 # 1 day
    echo "[entrypoint] yt-dlp update channel: nightly (pre-release builds — used at your own risk)"
else
    PIP_UPGRADE=(python3 -m pip install --no-cache-dir --upgrade yt-dlp yt-dlp-ejs)
    UPDATE_INTERVAL_SECONDS=604800 # 7 days
    echo "[entrypoint] yt-dlp update channel: stable"
fi

# requirements.txt pins yt-dlp/yt-dlp-ejs unversioned, but that only pulls
# latest at image build time — a long-lived container would otherwise keep
# whatever was installed then. YouTube changes break extraction (403s,
# missing/low-res formats) often enough that yt-dlp ships near-daily fixes,
# so re-check for an update on every start. Best-effort: a failed upgrade
# (e.g. offline) must not block startup with whatever version is already there.
echo "[entrypoint] Checking for yt-dlp updates..."
"${PIP_UPGRADE[@]}" \
    || echo "[entrypoint] yt-dlp upgrade check failed, continuing with installed version"

PID_FILE="/tmp/uvicorn.pid"
RESTART_FLAG="/tmp/uvicorn.restart"
STOPPING=0

start_server() {
    python3 -m uvicorn main:app --host 0.0.0.0 --port 3050 --workers 1 --log-level info &
    echo $! > "$PID_FILE"
}

trap 'STOPPING=1; kill -TERM "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null || true' SIGTERM SIGINT

# A found update restarts only the web server process (new process re-imports
# yt_dlp from disk) rather than the whole container.
(
    while true; do
        sleep "$UPDATE_INTERVAL_SECONDS"
        echo "[entrypoint] Scheduled yt-dlp update check..."
        BEFORE="$(python3 -m pip show yt-dlp 2>/dev/null | awk '/^Version:/{print $2}')"
        if ! "${PIP_UPGRADE[@]}"; then
            echo "[entrypoint] Scheduled yt-dlp upgrade failed, will retry next interval"
            continue
        fi
        AFTER="$(python3 -m pip show yt-dlp 2>/dev/null | awk '/^Version:/{print $2}')"
        if [ "$BEFORE" != "$AFTER" ]; then
            echo "[entrypoint] yt-dlp updated $BEFORE -> $AFTER, restarting web server..."
            touch "$RESTART_FLAG"
            kill -TERM "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null || true
        fi
    done
) &

echo "[entrypoint] Starting web server on :3050..."
start_server
while true; do
    set +e
    wait "$(cat "$PID_FILE")"
    EXIT_CODE=$?
    set -e
    if [ "$STOPPING" -eq 1 ]; then
        exit 0
    fi
    if [ -f "$RESTART_FLAG" ]; then
        rm -f "$RESTART_FLAG"
        start_server
        continue
    fi
    echo "[entrypoint] Web server exited unexpectedly (code $EXIT_CODE)"
    exit "$EXIT_CODE"
done

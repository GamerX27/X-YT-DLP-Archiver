# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A self-hosted web downloader powered by yt-dlp: paste a URL, pick a quality,
and a rule-based planner selects the format and destination folder
automatically. Single FastAPI backend + vanilla JS/CSS frontend, everything
running in one Docker container. No external API, cloud service, or ML
model involved anywhere in the planning/format-selection logic — it's all
plain Python rules driven off yt-dlp's own probed metadata.

## Commands

- **Run it**: `cp .env.example .env` then `docker compose up -d --build`
  (or `make up` / `make rebuild`). UI at `http://localhost:3050`.
- **Makefile** (`cd` here first): `make logs`, `make status`, `make health`,
  `make shell`, `make update-yt-dlp`, `make clean-tmp`, `make restart`,
  `make purge`. Run `make help` for the full list.
- **No test suite.** Verify changes by syntax-checking and exercising the
  running container — this is how changes were verified during development:
  ```
  python3 -m py_compile webui/*.py
  node --check webui/static/js/app.js
  bash -n webui/docker-entrypoint.sh
  docker compose up -d --build
  ```
  then actually drive it: `curl http://localhost:3050/api/health`, probe/
  download a real URL through the UI or via `curl -X POST .../api/probe`,
  and `docker compose logs -f` to watch it happen.
- Editing `webui/static/css/style.css` or `webui/static/js/app.js`? Bump the
  `?v=N` query param on that file's `<link>`/`<script>` tag in
  `webui/templates/index.html` — the browser won't otherwise see the change.

## Architecture

**Request flow**: `webui/main.py` (FastAPI routes + WebSocket `/ws`) is the
only entrypoint. A download becomes a `task` dict broadcast to every
connected client over the WebSocket as it moves through
`pending → probing → analyzing → downloading → moving/finalizing →
completed`; the frontend has no REST polling, it's entirely event-driven off
these broadcasts (see `updateTask`/the `ws.addEventListener("message", …)`
switch in `webui/static/js/app.js`). An `asyncio.Queue` + fixed-size worker
pool (`MAX_CONCURRENT_DOWNLOADS`) run `process_task()` for each queued task.

**The pipeline inside `process_task()`**: probe the URL with
`downloader.probe_url()` → `format_planner.analyze()` turns that metadata
into a format string + output template + destination folder (pure rules in
`webui/format_planner.py`: `_format_from_resolution`, `_folder_from_summary`,
`_folder_for_jellyfin` — no external service, edit these directly to change
codec/resolution/folder-layout rules) → `downloader.download()` runs yt-dlp
with progress callbacks → destination-specific finish step.

**Three destinations a task can end up at**, decided per-task in
`process_task()`:
1. **Jellyfin library** — moved into the library path, Jellyfin scan
   triggered (`webui/jellyfin.py`), TV-type playlists get their mtimes
   reordered to match playlist order afterward.
2. **Monitor** (`task["monitor_id"]` set) — same as Jellyfin above; monitors
   are *required* to have a Jellyfin destination (enforced both in the
   `/api/monitors` POST/PATCH handlers and in the frontend before it lets
   you submit the form) because a monitor runs unattended on a schedule and
   there's no browser session to hand a finished file to.
3. **Browser download** (no Jellyfin destination selected, not a monitor) —
   the file is never written to server disk long-term. It's staged under
   `DOWNLOADS_DIR/<task_id>/`, zipped if it's a playlist
   (`_prepare_browser_download` in `main.py`), and served through
   `GET /api/tasks/{id}/file`; the frontend auto-triggers the save via a
   synthetic `<a download>` click the moment the task completes (see
   `autoSaveToDevice` in `app.js`) — no button, no user action needed. A
   `cleanup_scheduler` background loop expires unclaimed staged files after
   `BROWSER_DOWNLOAD_TTL_HOURS`.

**Playlist resume/skip logic**: for playlist downloads, `download_archive`
tracks already-fetched video IDs. There's a *staging* archive (in the task's
temp dir, always used, so a long playlist can resume across multiple yt-dlp
passes within the same run — `_download_with_resume` in `main.py`) and,
only for Jellyfin/monitor destinations, a *persistent* archive committed to
the destination folder after a successful move (so a later run of the same
monitor/playlist skips what's already there). Browser downloads only get the
staging archive — there's no persistent destination to seed from or commit
to.

**Other backend modules**: `webui/monitor.py` is a small JSON-file-backed
store (`MonitorStore`) plus the schedule math (`_next_run` — handles hourly/
6h/12h/daily/weekly-with-a-specific-weekday) for the playlist-monitor
scheduler that runs in `main.py`'s `monitor_scheduler` background loop.
`webui/settings.py` is a generic key/value JSON store, currently used only
for the "default folder for YouTube Music links" setting. `webui/jellyfin.py`
also does host-path translation between the path Jellyfin's API reports and
the path this container actually has that library mounted at
(`JELLYFIN_MEDIA_PATH`).

**yt-dlp self-update** (`webui/docker-entrypoint.sh`): re-checks for a new
yt-dlp release at every container start, then keeps re-checking on an
interval in a background loop for as long as the container runs
(`YTDLP_BRANCH=stable` → weekly, `nightly` → daily + pre-release builds).
When an update actually installs, it restarts **only the uvicorn process**
in place (kills the tracked PID, `start_server` relaunches it) — the
container itself never restarts, since Docker's healthcheck/restart-count
would otherwise flicker. If you touch this script: the `wait` on the tracked
PID must stay bracketed in `set +e` / `set -e`, since under `set -euo
pipefail` a `wait` returning a SIGTERM exit code (143) otherwise aborts the
whole script — which used to silently degrade this into a full container
restart despite the in-place-restart logic looking correct on paper.

**Frontend**: one Jinja2 template (`webui/templates/index.html`) and one
script (`webui/static/js/app.js`), no build step, no framework. Two tabs:
Downloads (ad-hoc) and Monitor (playlist monitors) — the Monitor tab is
hidden entirely (`el.hidden`, checked via `/api/jellyfin/status`) unless
Jellyfin is configured, since monitors can't function without it. Tasks tied
to a monitor (`task.monitor_id`) are deliberately excluded from the
Downloads tab's Active/Completed lists — their progress only shows on that
monitor's own card, via `monitorTasks` keyed by monitor id.

## Notable environment variables

See `.env.example` / `README.md`'s Configuration table for the full list.
Ones worth knowing about while working in the code: `YOUTUBE_PLAYER_CLIENT`
is unset by default (yt-dlp picks its own client) — forcing a single client
can silently cap resolution if YouTube is throttling that client (see the
SABR-rollout warning logic in `downloader.py`'s `_check_resolution`).
`MEDIA_DIR`/`MEDIA_PATH` do not exist anymore — there is no server-side
default download folder; every non-Jellyfin download goes to the browser.

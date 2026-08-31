import asyncio
import errno
import json
import logging
import os
import re
import shutil
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from downloader import (
    DOWNLOADS_DIR,
    DownloadCancelled,
    Downloader,
    normalize_playlist_url,
    sanitize_path,
)
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.requests import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from format_planner import FormatPlanner
from jellyfin import JellyfinClient
from jinja2 import Environment, FileSystemLoader
from monitor import MonitorStore
from pydantic import BaseModel
from settings import SettingsStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
# Show DEBUG from our own modules only
logging.getLogger("format_planner").setLevel(logging.DEBUG)
logging.getLogger("downloader").setLevel(logging.DEBUG)
# Silence noisy third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
# Refuse to start a download when the staging (cache) drive has less free space
# than this. Long playlists fill it fast, and a full drive otherwise fails
# mid-download with a cryptic ENOSPC traceback. Configurable via env.
MIN_FREE_DISK_MB = int(os.getenv("MIN_FREE_DISK_MB", "2000"))
# How long a finished "browser download" (file staged for the user to save)
# is kept on the download cache drive before it's swept away automatically,
# in case the user never comes back to click "Save to device".
BROWSER_DOWNLOAD_TTL_HOURS = int(os.getenv("BROWSER_DOWNLOAD_TTL_HOURS", "12"))

# Statuses that mean a task still owns its playlist/URL — used to prevent
# enqueuing a duplicate download while one is already in flight.
ACTIVE_STATUSES = frozenset(
    {
        "pending",
        "probing",
        "analyzing",
        "downloading",
        "moving",
        "ordering",
        "finalizing",
    }
)

tasks: Dict[str, Dict[str, Any]] = {}
task_abort_events: Dict[str, threading.Event] = {}
ws_clients: List[WebSocket] = []
task_queue: asyncio.Queue = asyncio.Queue()
semaphore: Optional[asyncio.Semaphore] = None
worker_tasks: List[asyncio.Task] = []

downloader = Downloader()
format_planner = FormatPlanner()
jellyfin_client = JellyfinClient()
monitor_store = MonitorStore()
settings_store = SettingsStore()

DEFAULT_MUSIC_FOLDER_KEY = "default_music_folder"


async def broadcast(msg: Dict[str, Any]) -> None:
    dead = []
    data = json.dumps(msg)
    for ws in ws_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)


def update_task(task_id: str, **kwargs) -> None:
    if task_id not in tasks:
        return
    tasks[task_id].update(kwargs)
    asyncio.ensure_future(broadcast({"type": "task_update", "task": tasks[task_id]}))


def _active_task_for_url(url: str) -> Optional[Dict[str, Any]]:
    """Return an in-flight task already downloading ``url``, if any.

    Compares normalized playlist URLs so that e.g. a bare ``watch?v=…&list=…``
    and the full ``playlist?list=…`` form are treated as the same target.
    """
    target = normalize_playlist_url(url)
    for task in tasks.values():
        if task.get("status") not in ACTIVE_STATUSES:
            continue
        if normalize_playlist_url(task.get("url", "")) == target:
            return task
    return None


def _monitor_for_url(url: str) -> Optional[Dict[str, Any]]:
    """Return a monitor watching the same playlist as ``url``, if any."""
    target = normalize_playlist_url(url)
    for monitor in monitor_store.all():
        if normalize_playlist_url(monitor.get("url", "")) == target:
            return monitor
    return None


def _archive_count(monitor: Dict[str, Any]) -> int:
    """
    Count how many video IDs are in the download archive for this monitor.
    Returns 0 if the archive doesn't exist yet.
    """
    dest_base = monitor.get("jellyfin_library_path")
    if not dest_base:
        return 0

    # Use the stored folder_path if available (set after first download).
    # Fall back to an estimate using channel + playlist name.
    folder = monitor.get("monitor_folder_path")
    if not folder:
        channel = sanitize_path(monitor.get("channel") or "")
        name = sanitize_path(monitor.get("name") or "")
        if channel and name:
            folder = f"{channel}/{name}"
        elif name:
            folder = name
        else:
            return 0

    archive = Path(dest_base) / folder / ".yt-dlp-archive"
    try:
        if archive.exists():
            lines = [
                l for l in archive.read_text(encoding="utf-8").splitlines() if l.strip()
            ]
            return len(lines)
    except Exception:
        pass
    return 0


async def run_monitor(monitor: Dict[str, Any]) -> None:
    """Check for new videos and enqueue a download task if the playlist has grown."""
    monitor_id = monitor["id"]
    monitor_store.set_status(monitor_id, "checking")
    await broadcast({"type": "monitors_update", "monitors": monitor_store.all()})

    playlist_count = 0
    try:
        meta = await downloader.probe_url(monitor["url"])
        playlist_count = meta.get("playlist_count") or 0
    except Exception as exc:
        logger.warning("Monitor %s probe failed: %s", monitor_id, exc)

    archive_count = _archive_count(monitor)

    monitor_store.update(
        monitor_id,
        playlist_count=playlist_count,
        archive_count=archive_count,
    )
    await broadcast({"type": "monitors_update", "monitors": monitor_store.all()})

    if playlist_count > 0 and archive_count >= playlist_count:
        logger.info(
            "Monitor %s up-to-date (%d/%d)", monitor_id, archive_count, playlist_count
        )
        monitor_store.set_status(monitor_id, "up-to-date")
        monitor_store.mark_ran(monitor_id, new_videos=0)
        await broadcast({"type": "monitors_update", "monitors": monitor_store.all()})
        return

    # The download archive is only written as each video finishes, so during a
    # long playlist run archive_count stays below playlist_count for minutes.
    # Without this guard the 60 s scheduler would keep firing and enqueue a
    # duplicate task for the same playlist.
    existing = _active_task_for_url(monitor["url"])
    if existing is not None:
        logger.info(
            "Monitor %s already has an active task %s for this playlist; skipping",
            monitor_id,
            existing["id"],
        )
        monitor_store.set_status(monitor_id, "downloading")
        await broadcast({"type": "monitors_update", "monitors": monitor_store.all()})
        return

    task_id = str(uuid.uuid4())
    task = {
        "id": task_id,
        "url": monitor["url"],
        "status": "pending",
        "status_text": f"Monitor: {monitor.get('name', monitor['url'][:40])}",
        "progress": 0,
        "title": monitor.get("name"),
        "channel": monitor.get("channel"),
        "folder": None,
        "filename": None,
        "format_string": None,
        "speed": None,
        "eta": None,
        "playlist_index": None,
        "playlist_count": None,
        "final_path": None,
        "resolution_override": monitor.get("resolution_override", "1080p"),
        "jellyfin_library_id": monitor.get("jellyfin_library_id"),
        "jellyfin_library_name": monitor.get("jellyfin_library_name"),
        "jellyfin_library_path": monitor.get("jellyfin_library_path"),
        "jellyfin_library_type": monitor.get("jellyfin_library_type"),
        "folder_override": monitor.get("folder_override"),
        "monitor_id": monitor_id,
        "include_playlist_index": monitor.get("include_playlist_index", True),
        "error": None,
    }
    tasks[task_id] = task
    await task_queue.put(task_id)
    await broadcast({"type": "task_update", "task": task})
    logger.info(
        "Monitor %s enqueued task %s (%d in archive, %d in playlist)",
        monitor_id,
        task_id,
        archive_count,
        playlist_count,
    )


async def monitor_scheduler() -> None:
    """Background loop — checks every 60 s and triggers due monitors."""
    while True:
        await asyncio.sleep(60)
        try:
            for monitor in monitor_store.due():
                await run_monitor(monitor)
                monitor_store.mark_ran(monitor["id"])
        except Exception as exc:
            logger.exception("Monitor scheduler error: %s", exc)


async def cleanup_scheduler() -> None:
    """Background loop — expires "browser download" files nobody came back
    for, so the download cache drive doesn't fill up with abandoned files."""
    while True:
        await asyncio.sleep(3600)
        try:
            now = datetime.now()
            for task_id, task in list(tasks.items()):
                if not task.get("browser_ready"):
                    continue
                completed_at = task.get("_completed_at")
                if not completed_at:
                    continue
                try:
                    completed_dt = datetime.fromisoformat(completed_at)
                except ValueError:
                    continue
                if now - completed_dt > timedelta(hours=BROWSER_DOWNLOAD_TTL_HOURS):
                    shutil.rmtree(DOWNLOADS_DIR / task_id, ignore_errors=True)
                    tasks.pop(task_id, None)
                    await broadcast({"type": "task_removed", "task_id": task_id})
                    logger.info(
                        "Expired unclaimed browser download %s after %dh",
                        task_id,
                        BROWSER_DOWNLOAD_TTL_HOURS,
                    )
        except Exception:
            logger.exception("Cleanup scheduler error")


async def reorder_jellyfin_playlist(dest: Path, planner) -> None:
    """
    Normalize mtimes of downloaded playlist videos in dest so Jellyfin
    orders episodes by playlist index rather than upload date.
    Skipped if dates are already strictly ascending.
    """
    video_exts = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v"}
    files = sorted(
        f for f in dest.rglob("*") if f.is_file() and f.suffix.lower() in video_exts
    )
    if len(files) < 2:
        return

    idx_re = re.compile(r"^(\d+)")
    episodes = []
    for f in files:
        m = idx_re.match(f.name)
        idx = int(m.group(1)) if m else 9999
        try:
            mtime = f.stat().st_mtime
            date_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        except Exception:
            date_str = "2000-01-01"
        episodes.append(
            {
                "filename": f.name,
                "playlist_index": idx,
                "upload_date": date_str,
                "_path": f,
            }
        )

    episodes.sort(key=lambda e: e["playlist_index"])

    dates = [e["upload_date"] for e in episodes]
    if all(dates[i] < dates[i + 1] for i in range(len(dates) - 1)):
        logger.info("Jellyfin ordering: dates already strictly ascending — skipping")
        return

    episodes_input = [
        {
            "filename": e["filename"],
            "playlist_index": e["playlist_index"],
            "upload_date": e["upload_date"],
        }
        for e in episodes
    ]
    ordered = await planner.order_playlist_episodes(episodes_input)
    date_map = {item["filename"]: item["new_date"] for item in ordered}

    for ep in episodes:
        new_date = date_map.get(ep["filename"])
        if not new_date:
            continue
        # Update the embedded date tag AND the mtime together so Jellyfin's
        # displayed date and its sort order stay consistent.
        downloader.set_video_date(ep["_path"], new_date)
        logger.info("Jellyfin reorder: %s → %s", ep["filename"], new_date)


def _free_disk_mb(path: Path) -> int:
    """Free space (MB) on the filesystem holding ``path``; -1 if unknown."""
    try:
        return shutil.disk_usage(path).free // (1024 * 1024)
    except OSError:
        return -1


def _is_disk_full(exc: BaseException) -> bool:
    """True if an exception (or its chain) is a 'no space left on device' error.

    yt-dlp wraps the underlying ``OSError`` in a ``DownloadError`` whose message
    still carries the text, so check both the errno and the string form.
    """
    seen = exc
    while seen is not None:
        if isinstance(seen, OSError) and seen.errno == errno.ENOSPC:
            return True
        seen = seen.__cause__ or seen.__context__
    return "no space left on device" in str(exc).lower()


def _count_archive_entries(archive_path: Optional[str]) -> int:
    """Number of video IDs currently recorded in a yt-dlp download archive.

    Used to detect whether a download pass actually fetched anything new.
    """
    if not archive_path:
        return 0
    try:
        with open(archive_path, "r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


async def _download_with_resume(
    download_kwargs: Dict[str, Any],
    archive_file: str,
    task_id: str,
    max_passes: int = 12,
) -> Path:
    """Run the playlist download repeatedly until a pass downloads nothing new.

    YouTube can hand yt-dlp a truncated view of a long playlist (e.g. only the
    first ~100 of 181 entries), and long downloads can also be interrupted by
    transient network/format errors partway through. Because every fetched
    video ID is written to the persistent ``.yt-dlp-archive`` hidden file, we
    can simply run the same download again: yt-dlp skips everything already in
    the archive and continues with whatever remains. We loop until a clean pass
    adds zero new IDs (playlist complete) or we hit ``max_passes``.
    """
    last_dir = DOWNLOADS_DIR / task_id
    for pass_num in range(1, max_passes + 1):
        before = _count_archive_entries(archive_file)
        if pass_num > 1:
            update_task(
                task_id,
                status_text=f"Resuming playlist… (pass {pass_num}, {before} done)",
            )
        try:
            last_dir = await downloader.download(**download_kwargs)
        except DownloadCancelled:
            raise
        except Exception as exc:
            # A full cache drive won't recover by retrying — stop immediately
            # with a clear message instead of looping until max_passes.
            if _is_disk_full(exc):
                free_mb = _free_disk_mb(DOWNLOADS_DIR)
                raise RuntimeError(
                    "Download cache drive is full (no space left on device, "
                    f"{free_mb} MB free). Free up space and retry."
                ) from exc
            after = _count_archive_entries(archive_file)
            # If this pass still managed to fetch new items before failing,
            # resume on the next pass instead of giving up — the archive
            # guarantees we won't re-download what already succeeded.
            if after > before and pass_num < max_passes:
                logger.warning(
                    "Task %s — pass %d fetched %d new item(s) then errored, "
                    "resuming: %s",
                    task_id,
                    pass_num,
                    after - before,
                    exc,
                )
                continue
            raise

        after = _count_archive_entries(archive_file)
        new_items = after - before
        logger.info(
            "Task %s — playlist pass %d added %d new item(s) (archive %d → %d)",
            task_id,
            pass_num,
            new_items,
            before,
            after,
        )
        if new_items == 0:
            break
    return last_dir


async def _prepare_browser_download(task_dir: Path, folder_path: str) -> tuple:
    """Package a finished ad-hoc download for the browser to fetch.

    A single video/audio file is left as-is; a playlist (multiple files) is
    zipped as a whole so the user can save it in one click. Returns
    ``(file_path, filename)``.
    """
    safe_folder = "/".join(
        sanitize_path(p) for p in folder_path.split("/") if p.strip()
    )
    src_dir = task_dir / safe_folder if safe_folder else task_dir
    if not src_dir.is_dir():
        raise RuntimeError("Downloaded file could not be located")

    files = [f for f in src_dir.rglob("*") if f.is_file() and not f.name.startswith(".")]
    if not files:
        raise RuntimeError("Downloaded file could not be located")

    if len(files) == 1:
        return files[0], files[0].name

    zip_stem = sanitize_path(safe_folder.rstrip("/").split("/")[-1] or "playlist")
    base_name = str(task_dir / zip_stem)
    loop = asyncio.get_event_loop()
    zip_path_str = await loop.run_in_executor(
        None,
        lambda: shutil.make_archive(
            base_name, "zip", root_dir=str(src_dir.parent), base_dir=src_dir.name
        ),
    )
    zip_path = Path(zip_path_str)
    shutil.rmtree(src_dir, ignore_errors=True)
    return zip_path, zip_path.name


async def process_task(task_id: str) -> None:
    task = tasks.get(task_id)
    if task is None:
        return

    abort_event = threading.Event()
    task_abort_events[task_id] = abort_event

    def _cancelled() -> bool:
        if abort_event.is_set():
            shutil.rmtree(DOWNLOADS_DIR / task_id, ignore_errors=True)
            update_task(
                task_id, status="cancelled", status_text="Cancelled", progress=0
            )
            return True
        return False

    try:
        if _cancelled():
            return
        resolved_url = normalize_playlist_url(task["url"])
        if resolved_url != task["url"]:
            logger.info("Task %s — using full playlist URL: %s", task_id, resolved_url)
        else:
            logger.info("Task %s — starting: %s", task_id, resolved_url)
        update_task(task_id, status="probing", status_text="Probing URL…")
        metadata = await downloader.probe_url(task["url"])
        title = metadata.get("title") or task["url"]
        channel = metadata.get("channel") or metadata.get("uploader") or "Unknown"
        update_task(task_id, title=title, channel=channel)

        if _cancelled():
            return
        update_task(task_id, status="analyzing", status_text="Planning download…")
        plan = await format_planner.analyze(
            metadata,
            task.get("resolution_override"),
            jellyfin_library_type=task.get("jellyfin_library_type"),
            include_playlist_index=task.get("include_playlist_index", True),
        )
        update_task(
            task_id,
            folder=plan.get("folder_path", "Downloads"),
            format_string=plan.get("format_string", "bestvideo+bestaudio/best"),
            status_text=f"Plan ready: {plan.get('reasoning', '')}",
        )

        # If the user manually selected a destination subfolder in the browser,
        # override the computed folder and use a simple filename template.
        if task.get("folder_override"):
            plan["folder_path"] = task["folder_override"]
            # For a single track going into an existing folder, keep the title only.
            if plan.get("content_type") != "playlist":
                plan["output_template"] = "%(title)s.%(ext)s"
            update_task(task_id, folder=plan["folder_path"])

        # For playlists: use a persistent download archive so yt-dlp skips
        # videos already downloaded on previous runs of the same playlist.
        #
        # The persistent archive lives in the DESTINATION folder, but we only
        # commit IDs to it AFTER their files have actually been moved there.
        # Videos download into a temporary staging dir first; if the run fails
        # between download and move (e.g. the cache drive fills up), writing the
        # archive directly in the destination would record IDs whose files never
        # arrived — permanently skipping those episodes on the next run. So
        # during the download we use a *staging* archive seeded from the
        # persistent one, and commit it only on success (see after the move).
        # A download only gets a persistent server-side destination (and thus a
        # persistent cross-run archive) when it's writing into a Jellyfin
        # library. An ad-hoc download with none selected streams straight to
        # the user's browser instead — there is nothing on disk to seed an
        # archive from, and nothing worth keeping around after the user saves
        # the file. Monitors always have a Jellyfin destination (the Monitor
        # tab is unusable without Jellyfin configured — see below).
        browser_download = not task.get("jellyfin_library_path") and not task.get(
            "monitor_id"
        )

        if task.get("monitor_id") and not task.get("jellyfin_library_path"):
            msg = "This monitor has no Jellyfin destination configured."
            update_task(task_id, status="failed", status_text=msg, error=msg)
            return

        dest_archive: Optional[Path] = None
        staging_archive: Optional[Path] = None
        if plan.get("content_type") == "playlist":
            staging_archive = DOWNLOADS_DIR / task_id / ".yt-dlp-archive"
            try:
                staging_archive.parent.mkdir(parents=True, exist_ok=True)
                if not browser_download:
                    archive_base = Path(task["jellyfin_library_path"])
                    dest_archive = (
                        archive_base / plan.get("folder_path", "") / ".yt-dlp-archive"
                    )
                    # Seed the staging archive with everything already in the
                    # destination so we don't re-download completed episodes.
                    if dest_archive.exists():
                        shutil.copyfile(dest_archive, staging_archive)
                    logger.info(
                        "Playlist archive: staging=%s commits to %s after move",
                        staging_archive,
                        dest_archive,
                    )
                plan["extra_opts"]["download_archive"] = str(staging_archive)
            except Exception as exc:
                logger.warning("Could not set up download archive (non-fatal): %s", exc)

        if _cancelled():
            return

        # Preflight: refuse to start if the staging drive is already low on
        # space, so we fail fast with a clear message instead of part-way
        # through with an ENOSPC error (and a half-written staging dir).
        free_mb = _free_disk_mb(DOWNLOADS_DIR)
        if 0 <= free_mb < MIN_FREE_DISK_MB:
            msg = (
                f"Not enough free disk space on the download cache drive: "
                f"{free_mb} MB free, need at least {MIN_FREE_DISK_MB} MB."
            )
            logger.error("Task %s — %s", task_id, msg)
            update_task(
                task_id, status="failed", status_text=msg, error=msg, progress=0
            )
            return

        is_audio = task.get("resolution_override") == "audio"
        update_task(task_id, status="downloading", status_text="Downloading…")

        async def progress_cb(
            percent: float,
            speed: str,
            eta: str,
            filename: str,
            playlist_index: Optional[int] = None,
            playlist_count: Optional[int] = None,
        ) -> None:
            if playlist_index and playlist_count:
                status_text = f"{playlist_index}/{playlist_count} · {percent:.1f}%"
            else:
                status_text = f"Downloading… {percent:.1f}%"
            update_task(
                task_id,
                progress=percent,
                speed=speed,
                eta=eta,
                filename=filename,
                playlist_index=playlist_index,
                playlist_count=playlist_count,
                status_text=status_text,
            )

        download_kwargs: Dict[str, Any] = dict(
            url=task["url"],
            format_string=plan.get("format_string", "bestvideo+bestaudio/best"),
            output_template=plan.get("output_template", "%(title)s [%(id)s].%(ext)s"),
            extra_opts=plan.get("extra_opts", {}),
            folder_path=plan.get("folder_path", "Downloads"),
            task_id=task_id,
            progress_cb=progress_cb,
            abort_event=abort_event,
            is_audio=is_audio,
            resolution_override=task.get("resolution_override"),
        )

        # For playlists with a download archive, keep re-running the download
        # until a pass adds nothing new. yt-dlp skips already-archived IDs, so
        # each pass resumes where the previous one left off — completing
        # playlists that YouTube only exposed partially and recovering from
        # mid-playlist interruptions.
        archive_file = plan.get("extra_opts", {}).get("download_archive")
        try:
            if plan.get("content_type") == "playlist" and archive_file:
                task_dir = await _download_with_resume(
                    download_kwargs, archive_file, task_id
                )
            else:
                task_dir = await downloader.download(**download_kwargs)
        except DownloadCancelled:
            shutil.rmtree(DOWNLOADS_DIR / task_id, ignore_errors=True)
            update_task(
                task_id, status="cancelled", status_text="Cancelled", progress=0
            )
            return

        if _cancelled():
            return

        if browser_download:
            # No Jellyfin library configured for this download — stage the
            # finished file(s) in place and let the user pull it down through
            # the browser instead of writing it to the server.
            update_task(
                task_id,
                status="finalizing",
                status_text="Preparing file…",
                progress=100,
            )
            file_path, download_filename = await _prepare_browser_download(
                task_dir, plan.get("folder_path", "Downloads")
            )
            logger.info("Task %s ready for browser download: %s", task_id, file_path)
            update_task(
                task_id,
                status="completed",
                status_text="Ready for download",
                final_path=None,
                browser_ready=True,
                download_filename=download_filename,
                progress=100,
                _completed_at=datetime.now().isoformat(),
            )
            return

        # Guaranteed set here: browser_download (no Jellyfin, no monitor) and
        # monitor-without-Jellyfin both returned above.
        dest_dir = task["jellyfin_library_path"]

        update_task(
            task_id,
            status="moving",
            status_text="Moving to Jellyfin folder…",
            progress=100,
        )

        # Snapshot the staging archive before the move (which deletes the
        # staging dir). We commit it to the destination only after the move
        # succeeds, so the archive can never get ahead of the files on disk.
        archive_snapshot: Optional[str] = None
        if staging_archive is not None and staging_archive.exists():
            try:
                archive_snapshot = staging_archive.read_text(encoding="utf-8")
            except OSError:
                archive_snapshot = None

        final_dest = await downloader.move_to_media(
            task_dir=task_dir,
            folder_path=plan.get("folder_path", "Downloads"),
            media_dir=dest_dir,
        )

        # Commit the archive now that the files are actually in the destination.
        if archive_snapshot is not None and dest_archive is not None:
            try:
                dest_archive.parent.mkdir(parents=True, exist_ok=True)
                dest_archive.write_text(archive_snapshot, encoding="utf-8")
            except OSError as exc:
                logger.warning("Could not commit download archive (non-fatal): %s", exc)

        # Reorder before triggering the library scan below — Jellyfin ingests
        # each file's date at scan time, so fixing mtimes after the scan has
        # already run leaves the wrong order cached in Jellyfin's database
        # until some later, unrelated rescan happens to pick it up.
        if (
            plan.get("content_type") == "playlist"
            and task.get("jellyfin_library_path")
            and not is_audio
        ):
            update_task(
                task_id,
                status="ordering",
                status_text="Ordering episodes for Jellyfin…",
            )
            try:
                await reorder_jellyfin_playlist(final_dest, format_planner)
            except Exception as exc:
                logger.warning(
                    "Jellyfin episode reordering failed (non-fatal): %s", exc
                )

        jf_lib_id = task.get("jellyfin_library_id")
        if jf_lib_id:
            try:
                await jellyfin_client.refresh_library(jf_lib_id)
                logger.info("Jellyfin library %s refresh triggered", jf_lib_id)
            except Exception as jf_exc:
                logger.warning("Jellyfin refresh failed (non-fatal): %s", jf_exc)

        update_task(
            task_id,
            status="completed",
            status_text="Done",
            final_path=str(final_dest),
            progress=100,
        )

        # If triggered by a monitor, record the exact folder_path used so
        # future archive checks know exactly where to look.
        if task.get("monitor_id"):
            _mon = monitor_store.get(task["monitor_id"]) or {}
            fresh_count = _archive_count(
                {**_mon, "monitor_folder_path": plan.get("folder_path")}
            )
            monitor_store.update(
                task["monitor_id"],
                monitor_folder_path=plan.get("folder_path"),
                archive_count=fresh_count,
                last_new_videos=max(0, fresh_count - _mon.get("archive_count", 0)),
            )
            await broadcast(
                {"type": "monitors_update", "monitors": monitor_store.all()}
            )

    except Exception as exc:
        if _is_disk_full(exc):
            free_mb = _free_disk_mb(DOWNLOADS_DIR)
            msg = (
                f"Download cache drive is full (no space left on device, "
                f"{free_mb} MB free). Free up space and retry."
            )
            logger.error("Task %s failed — %s", task_id, msg)
            update_task(task_id, status="failed", status_text=msg, error=msg)
        else:
            logger.exception("Task %s failed", task_id)
            update_task(task_id, status="failed", status_text="Failed", error=str(exc))
    finally:
        task_abort_events.pop(task_id, None)


async def worker() -> None:
    while True:
        task_id = await task_queue.get()
        async with semaphore:
            try:
                await process_task(task_id)
            except Exception:
                logger.exception("Unhandled error in worker for task %s", task_id)
            finally:
                task_queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global semaphore
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    for _ in range(MAX_CONCURRENT_DOWNLOADS):
        t = asyncio.create_task(worker())
        worker_tasks.append(t)
    worker_tasks.append(asyncio.create_task(monitor_scheduler()))
    worker_tasks.append(asyncio.create_task(cleanup_scheduler()))
    yield
    for t in worker_tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)

# Build the Jinja2 environment manually with cache_size=0 to work around a
# Python 3.14 incompatibility in Jinja2's LRU cache (unhashable dict in key tuple).
_templates_dir = os.path.join(os.path.dirname(__file__), "templates")
_jinja_env = Environment(
    loader=FileSystemLoader(_templates_dir),
    cache_size=0,
    autoescape=True,
)
templates = Jinja2Templates(env=_jinja_env)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Browsers request /favicon.ico directly regardless of <link rel="icon">;
    # redirect to the real asset instead of returning a 404.
    return RedirectResponse(url="/static/favicon.ico")


class DownloadRequest(BaseModel):
    url: str
    resolution_override: Optional[str] = None
    jellyfin_library_id: Optional[str] = None
    jellyfin_library_name: Optional[str] = None
    jellyfin_library_path: Optional[str] = None
    jellyfin_library_type: Optional[str] = None
    folder_override: Optional[str] = (
        None  # relative subfolder within the library (user-selected)
    )
    include_playlist_index: Optional[bool] = True


class ProbeRequest(BaseModel):
    url: str


@app.post("/api/probe")
async def api_probe(body: ProbeRequest):
    """Probe a URL and return which of the three resolution tiers are available."""
    is_music = "music.youtube.com" in body.url

    try:
        metadata = await downloader.probe_url(body.url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    is_playlist = metadata.get("_type") == "playlist"
    title = metadata.get("title", "")
    channel = metadata.get("channel") or metadata.get("uploader", "")
    thumbnail = metadata.get("thumbnail")
    if not thumbnail:
        thumbs = metadata.get("thumbnails") or []
        if thumbs:
            thumbnail = thumbs[-1].get("url")

    if is_music:
        # Music URLs: audio-only mode, no resolution selection needed
        return {
            "title": title,
            "channel": channel,
            "is_playlist": is_playlist,
            "available": [],
            "is_audio": True,
            "thumbnail": thumbnail,
        }

    if is_playlist:
        available = ["720p", "1080p", "1440p", "2160p"]
    else:
        formats = metadata.get("formats") or []
        heights = sorted(
            {
                f.get("height", 0)
                for f in formats
                if isinstance(f.get("height"), int) and f["height"] > 0
            },
            reverse=True,
        )
        max_h = heights[0] if heights else 0

        available = []
        if max_h >= 720:
            available.append("720p")
        if max_h >= 1080:
            available.append("1080p")
        if max_h >= 1440:
            available.append("1440p")
        if max_h >= 2160:
            available.append("2160p")
        if not available:
            available = ["720p"]

    return {
        "title": title,
        "channel": channel,
        "is_playlist": is_playlist,
        "available": available,
        "is_audio": False,
        "thumbnail": thumbnail,
    }


@app.get("/api/jellyfin/status")
async def api_jellyfin_status():
    return {"enabled": jellyfin_client.enabled}


@app.get("/api/jellyfin/libraries")
async def api_jellyfin_libraries():
    try:
        libs = await jellyfin_client.get_libraries()
        return {"libraries": libs}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Jellyfin error: {exc}")


@app.get("/api/browse")
async def api_browse(path: str):
    """List immediate subdirectories of a local path (for the music folder browser)."""
    try:
        target = Path(path)
        if not target.is_dir():
            return {"folders": []}
        folders = sorted(
            d.name
            for d in target.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
        return {"folders": folders}
    except Exception:
        return {"folders": []}


class DefaultMusicFolderRequest(BaseModel):
    jellyfin_library_id: str
    jellyfin_library_name: Optional[str] = None
    jellyfin_library_path: Optional[str] = None
    jellyfin_library_type: Optional[str] = None
    folder_override: Optional[str] = None  # relative subfolder within the library


@app.get("/api/settings/default-music-folder")
async def api_get_default_music_folder():
    return settings_store.get(DEFAULT_MUSIC_FOLDER_KEY)


@app.post("/api/settings/default-music-folder")
async def api_set_default_music_folder(body: DefaultMusicFolderRequest):
    return settings_store.set(DEFAULT_MUSIC_FOLDER_KEY, body.model_dump())


@app.delete("/api/settings/default-music-folder")
async def api_clear_default_music_folder():
    settings_store.clear(DEFAULT_MUSIC_FOLDER_KEY)
    return {"cleared": True}


@app.post("/api/download")
async def api_download(body: DownloadRequest):
    existing = _active_task_for_url(body.url)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="This playlist is already downloading.",
        )

    # If this playlist is already watched by a monitor, route the download to
    # the monitor's Jellyfin destination so the same playlist can't diverge
    # into two folders/archives. The user's explicit form choices still win.
    monitor = _monitor_for_url(body.url)
    is_music_link = "music.youtube.com" in body.url
    if monitor is None and is_music_link and not body.jellyfin_library_id:
        # No monitor and no explicit destination — fall back to the user's
        # configured default music folder, if one was set (see
        # /api/settings/default-music-folder). Mirrors the monitor-routing
        # logic above so calling /api/download directly still benefits from it.
        default_folder = settings_store.get(DEFAULT_MUSIC_FOLDER_KEY)
        if default_folder:
            body.jellyfin_library_id = default_folder.get("jellyfin_library_id")
            body.jellyfin_library_name = default_folder.get("jellyfin_library_name")
            body.jellyfin_library_path = default_folder.get("jellyfin_library_path")
            body.jellyfin_library_type = default_folder.get("jellyfin_library_type")
            if not body.folder_override:
                body.folder_override = default_folder.get("folder_override")
    if monitor is not None:
        if not body.jellyfin_library_id and monitor.get("jellyfin_library_id"):
            body.jellyfin_library_id = monitor.get("jellyfin_library_id")
            body.jellyfin_library_name = monitor.get("jellyfin_library_name")
            body.jellyfin_library_path = monitor.get("jellyfin_library_path")
            body.jellyfin_library_type = monitor.get("jellyfin_library_type")
        if not body.folder_override:
            # Prefer the exact folder the monitor has been writing to so the
            # archive lines up; fall back to its configured override.
            body.folder_override = monitor.get("monitor_folder_path") or monitor.get(
                "folder_override"
            )
        if not body.resolution_override and monitor.get("resolution_override"):
            body.resolution_override = monitor.get("resolution_override")
        logger.info(
            "Manual download for %s matched monitor %s; routing to its destination",
            body.url,
            monitor["id"],
        )

    task_id = str(uuid.uuid4())
    task = {
        "id": task_id,
        "url": body.url,
        "status": "pending",
        "status_text": "Queued",
        "progress": 0,
        "title": None,
        "channel": None,
        "folder": None,
        "filename": None,
        "format_string": None,
        "speed": None,
        "eta": None,
        "playlist_index": None,
        "playlist_count": None,
        "final_path": None,
        "resolution_override": body.resolution_override,
        "jellyfin_library_id": body.jellyfin_library_id,
        "jellyfin_library_name": body.jellyfin_library_name,
        "jellyfin_library_path": body.jellyfin_library_path,
        "jellyfin_library_type": body.jellyfin_library_type,
        "folder_override": body.folder_override,
        "include_playlist_index": body.include_playlist_index,
        # Tag with the matching monitor (if any) so it routes to the same
        # destination/archive and its progress shows on that monitor's card
        # instead of cluttering the main downloads list.
        "monitor_id": monitor["id"] if monitor is not None else None,
        "error": None,
    }
    tasks[task_id] = task
    await task_queue.put(task_id)
    await broadcast({"type": "task_update", "task": task})
    return task


@app.get("/api/tasks")
async def api_tasks():
    return list(tasks.values())


@app.get("/api/tasks/{task_id}/file")
async def api_download_task_file(task_id: str):
    """Serve a finished ad-hoc download's file for the browser to save."""
    task = tasks.get(task_id)
    if not task or not task.get("browser_ready"):
        raise HTTPException(status_code=404, detail="File not available")
    filename = task.get("download_filename")
    if not filename:
        raise HTTPException(status_code=404, detail="File not available")

    task_dir = DOWNLOADS_DIR / task_id
    match: Optional[Path] = None
    if task_dir.is_dir():
        for f in task_dir.rglob(filename):
            if f.is_file():
                match = f
                break
    if not match:
        raise HTTPException(
            status_code=404, detail="File no longer available — it may have expired"
        )

    media_type = "application/zip" if match.suffix.lower() == ".zip" else "application/octet-stream"
    return FileResponse(path=str(match), filename=filename, media_type=media_type)


class MonitorRequest(BaseModel):
    url: str
    name: Optional[str] = None
    schedule: str = "daily"
    schedule_time: str = "03:00"
    schedule_day: Optional[int] = None  # 0=Monday…6=Sunday, used when schedule="weekly"
    resolution_override: Optional[str] = "1080p"
    jellyfin_library_id: Optional[str] = None
    jellyfin_library_name: Optional[str] = None
    jellyfin_library_path: Optional[str] = None
    jellyfin_library_type: Optional[str] = None
    folder_override: Optional[str] = None
    enabled: bool = True
    include_playlist_index: bool = True


@app.get("/api/monitors")
async def api_monitors():
    return monitor_store.all()


@app.post("/api/monitors")
async def api_add_monitor(body: MonitorRequest):
    # Monitors run unattended on a schedule — there's no browser to hand a
    # finished file to, so they need a real, persistent Jellyfin destination.
    if not body.jellyfin_library_id:
        raise HTTPException(
            status_code=400, detail="A Jellyfin library is required to create a monitor"
        )
    name = body.name
    channel = None
    if not name:
        try:
            meta = await downloader.probe_url(body.url)
            name = meta.get("title") or meta.get("playlist_title") or body.url
            channel = meta.get("channel") or meta.get("uploader")
        except Exception:
            name = body.url
    monitor = monitor_store.add(
        {
            "url": body.url,
            "name": name,
            "channel": channel,
            "schedule": body.schedule,
            "schedule_time": body.schedule_time,
            "schedule_day": body.schedule_day,
            "resolution_override": body.resolution_override,
            "jellyfin_library_id": body.jellyfin_library_id,
            "jellyfin_library_name": body.jellyfin_library_name,
            "jellyfin_library_path": body.jellyfin_library_path,
            "jellyfin_library_type": body.jellyfin_library_type,
            "folder_override": body.folder_override,
            "enabled": body.enabled,
            "include_playlist_index": body.include_playlist_index,
        }
    )
    await broadcast({"type": "monitors_update", "monitors": monitor_store.all()})
    return monitor


@app.delete("/api/monitors/{monitor_id}")
async def api_delete_monitor(monitor_id: str):
    if not monitor_store.remove(monitor_id):
        raise HTTPException(status_code=404, detail="Monitor not found")
    await broadcast({"type": "monitors_update", "monitors": monitor_store.all()})
    return {"deleted": monitor_id}


@app.post("/api/monitors/{monitor_id}/run")
async def api_run_monitor_now(monitor_id: str):
    monitor = monitor_store.get(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    await run_monitor(monitor)
    monitor_store.mark_ran(monitor_id)
    await broadcast({"type": "monitors_update", "monitors": monitor_store.all()})
    return {"queued": monitor_id}


@app.patch("/api/monitors/{monitor_id}")
async def api_update_monitor(monitor_id: str, body: dict):
    if "jellyfin_library_id" in body and not body["jellyfin_library_id"]:
        raise HTTPException(
            status_code=400, detail="A Jellyfin library is required for monitors"
        )
    monitor = monitor_store.update(monitor_id, **body)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    await broadcast({"type": "monitors_update", "monitors": monitor_store.all()})
    return monitor


@app.post("/api/tasks/{task_id}/cancel")
async def api_cancel_task(task_id: str):
    """Signal a running download to stop and clean up its partial files."""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    abort_event = task_abort_events.get(task_id)
    if abort_event is not None:
        abort_event.set()
    update_task(task_id, status="cancelling", status_text="Cancelling…")
    return {"cancelling": task_id}


@app.delete("/api/tasks/{task_id}")
async def api_delete_task(task_id: str):
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    task = tasks.pop(task_id)
    if task.get("browser_ready"):
        # This task's file lives only in the download cache, not a library —
        # clean it up now instead of waiting for the TTL sweep.
        shutil.rmtree(DOWNLOADS_DIR / task_id, ignore_errors=True)
    await broadcast({"type": "task_removed", "task_id": task_id})
    return {"deleted": task_id}


@app.get("/api/health")
async def api_health():
    free_mb = _free_disk_mb(DOWNLOADS_DIR)
    return {
        "status": "ok",
        "disk_free_mb": free_mb,
        "disk_min_mb": MIN_FREE_DISK_MB,
        # True when there isn't enough room to safely start a new download.
        "disk_low": 0 <= free_mb < MIN_FREE_DISK_MB,
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.append(websocket)
    try:
        await websocket.send_text(
            json.dumps({"type": "init", "tasks": list(tasks.values())})
        )
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in ws_clients:
            ws_clients.remove(websocket)

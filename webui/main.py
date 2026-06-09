import asyncio
import json
import logging
import os
import re
import shutil
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from downloader import DOWNLOADS_DIR, DownloadCancelled, Downloader
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jellyfin import JellyfinClient
from jinja2 import Environment, FileSystemLoader
from llm_agent import LLMAgent
from monitor import MonitorStore
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
# Show DEBUG from our own modules only
logging.getLogger("llm_agent").setLevel(logging.DEBUG)
logging.getLogger("downloader").setLevel(logging.DEBUG)
# Silence noisy third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
MEDIA_DIR = os.getenv(
    "MEDIA_DIR", "/media"
)  # matches the volume mount in docker-compose

tasks: Dict[str, Dict[str, Any]] = {}
task_abort_events: Dict[str, threading.Event] = {}
ws_clients: List[WebSocket] = []
task_queue: asyncio.Queue = asyncio.Queue()
semaphore: Optional[asyncio.Semaphore] = None
worker_tasks: List[asyncio.Task] = []

downloader = Downloader()
llm_agent = LLMAgent()
jellyfin_client = JellyfinClient()
monitor_store = MonitorStore()


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


async def run_monitor(monitor: Dict[str, Any]) -> None:
    """Enqueue a download task for a monitor; archive ensures only new videos."""
    monitor_id = monitor["id"]
    monitor_store.set_status(monitor_id, "checking")
    await broadcast({"type": "monitors_update", "monitors": monitor_store.all()})

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
        "jellyfin_library_path": monitor.get("jellyfin_library_path"),
        "jellyfin_library_type": monitor.get("jellyfin_library_type"),
        "folder_override": monitor.get("folder_override"),
        "monitor_id": monitor_id,
        "error": None,
    }
    tasks[task_id] = task
    await task_queue.put(task_id)
    await broadcast({"type": "task_update", "task": task})
    logger.info("Monitor %s enqueued task %s", monitor_id, task_id)


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


async def reorder_jellyfin_playlist(dest: Path, agent) -> None:
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

    llm_input = [
        {
            "filename": e["filename"],
            "playlist_index": e["playlist_index"],
            "upload_date": e["upload_date"],
        }
        for e in episodes
    ]
    ordered = await agent.order_playlist_episodes(llm_input)
    date_map = {item["filename"]: item["new_date"] for item in ordered}

    for ep in episodes:
        new_date = date_map.get(ep["filename"])
        if not new_date:
            continue
        try:
            dt = datetime.strptime(new_date, "%Y-%m-%d")
            ts = dt.timestamp()
            os.utime(ep["_path"], (ts, ts))
            logger.info("Jellyfin reorder: %s → %s", ep["filename"], new_date)
        except Exception as exc:
            logger.warning("utime failed for %s: %s", ep["filename"], exc)


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
        # Step 1: Probe
        if _cancelled():
            return
        update_task(task_id, status="probing", status_text="Probing URL…")
        metadata = await downloader.probe_url(task["url"])
        title = metadata.get("title") or task["url"]
        channel = metadata.get("channel") or metadata.get("uploader") or "Unknown"
        update_task(task_id, title=title, channel=channel)

        # Step 2: Analyze with LLM
        if _cancelled():
            return
        update_task(task_id, status="analyzing", status_text="Analyzing with LLM…")
        plan = await llm_agent.analyze(
            metadata,
            task.get("resolution_override"),
            jellyfin_library_type=task.get("jellyfin_library_type"),
        )
        update_task(
            task_id,
            folder=plan.get("folder_path", "Downloads"),
            format_string=plan.get("format_string", "bestvideo+bestaudio/best"),
            status_text=f"Plan ready: {plan.get('reasoning', '')}",
        )

        # If the user manually selected a destination subfolder in the browser,
        # override the LLM's folder and use a simple filename template.
        if task.get("folder_override"):
            plan["folder_path"] = task["folder_override"]
            # For a single track going into an existing folder, keep the title only.
            if plan.get("content_type") != "playlist":
                plan["output_template"] = "%(title)s.%(ext)s"
            update_task(task_id, folder=plan["folder_path"])

        # For playlists: set up a persistent download archive in the destination
        # folder so yt-dlp can skip videos that were already downloaded on
        # previous runs of the same playlist.
        if plan.get("content_type") == "playlist":
            jf_path_for_archive = task.get("jellyfin_library_path")
            archive_base = Path(
                jf_path_for_archive if jf_path_for_archive else MEDIA_DIR
            )
            archive_dir = archive_base / plan.get("folder_path", "")
            try:
                archive_dir.mkdir(parents=True, exist_ok=True)
                archive_file = archive_dir / ".yt-dlp-archive"
                plan["extra_opts"]["download_archive"] = str(archive_file)
                logger.info("Playlist download archive: %s", archive_file)
            except Exception as exc:
                logger.warning("Could not create archive dir (non-fatal): %s", exc)

        # Step 3: Download
        if _cancelled():
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

        try:
            task_dir = await downloader.download(
                url=task["url"],
                format_string=plan.get("format_string", "bestvideo+bestaudio/best"),
                output_template=plan.get(
                    "output_template", "%(title)s [%(id)s].%(ext)s"
                ),
                extra_opts=plan.get("extra_opts", {}),
                folder_path=plan.get("folder_path", "Downloads"),
                task_id=task_id,
                progress_cb=progress_cb,
                abort_event=abort_event,
                is_audio=is_audio,
            )
        except DownloadCancelled:
            shutil.rmtree(DOWNLOADS_DIR / task_id, ignore_errors=True)
            update_task(
                task_id, status="cancelled", status_text="Cancelled", progress=0
            )
            return

        # Step 4: Move
        if _cancelled():
            return
        jellyfin_path = task.get("jellyfin_library_path")
        dest_dir = jellyfin_path if jellyfin_path else MEDIA_DIR

        update_task(
            task_id,
            status="moving",
            status_text=f"Moving to {'Jellyfin' if jellyfin_path else 'media'} folder…",
            progress=100,
        )
        final_dest = await downloader.move_to_media(
            task_dir=task_dir,
            folder_path=plan.get("folder_path", "Downloads"),
            media_dir=dest_dir,
        )

        # Trigger Jellyfin library scan after move
        jf_lib_id = task.get("jellyfin_library_id")
        if jf_lib_id:
            try:
                await jellyfin_client.refresh_library(jf_lib_id)
                logger.info("Jellyfin library %s refresh triggered", jf_lib_id)
            except Exception as jf_exc:
                logger.warning("Jellyfin refresh failed (non-fatal): %s", jf_exc)

        # Step 5: Reorder episodes for Jellyfin (video playlist only, skip for audio)
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
                await reorder_jellyfin_playlist(final_dest, llm_agent)
            except Exception as exc:
                logger.warning(
                    "Jellyfin episode reordering failed (non-fatal): %s", exc
                )

        update_task(
            task_id,
            status="completed",
            status_text="Done",
            final_path=str(final_dest),
            progress=100,
        )

    except Exception as exc:
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
    try:
        await llm_agent.ensure_model()
    except Exception:
        logger.warning("ensure_model failed during startup; continuing anyway")
    for _ in range(MAX_CONCURRENT_DOWNLOADS):
        t = asyncio.create_task(worker())
        worker_tasks.append(t)
    worker_tasks.append(asyncio.create_task(monitor_scheduler()))
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


# ── Routes ──────────────────────────────────────────────────────────────────


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


class DownloadRequest(BaseModel):
    url: str
    resolution_override: Optional[str] = None
    jellyfin_library_id: Optional[str] = None
    jellyfin_library_path: Optional[str] = None
    jellyfin_library_type: Optional[str] = None
    folder_override: Optional[str] = (
        None  # relative subfolder within the library (user-selected)
    )


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

    if is_music:
        # Music URLs: audio-only mode, no resolution selection needed
        return {
            "title": title,
            "channel": channel,
            "is_playlist": is_playlist,
            "available": [],
            "is_audio": True,
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


@app.post("/api/download")
async def api_download(body: DownloadRequest):
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
        "jellyfin_library_path": body.jellyfin_library_path,
        "jellyfin_library_type": body.jellyfin_library_type,
        "folder_override": body.folder_override,
        "error": None,
    }
    tasks[task_id] = task
    await task_queue.put(task_id)
    await broadcast({"type": "task_update", "task": task})
    return task


@app.get("/api/tasks")
async def api_tasks():
    return list(tasks.values())


# ── Monitor routes ───────────────────────────────────────────────────────────


class MonitorRequest(BaseModel):
    url: str
    name: Optional[str] = None
    schedule: str = "daily"
    schedule_time: str = "03:00"
    resolution_override: Optional[str] = "1080p"
    jellyfin_library_id: Optional[str] = None
    jellyfin_library_path: Optional[str] = None
    jellyfin_library_type: Optional[str] = None
    folder_override: Optional[str] = None
    enabled: bool = True


@app.get("/api/monitors")
async def api_monitors():
    return monitor_store.all()


@app.post("/api/monitors")
async def api_add_monitor(body: MonitorRequest):
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
            "resolution_override": body.resolution_override,
            "jellyfin_library_id": body.jellyfin_library_id,
            "jellyfin_library_path": body.jellyfin_library_path,
            "jellyfin_library_type": body.jellyfin_library_type,
            "folder_override": body.folder_override,
            "enabled": body.enabled,
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
    del tasks[task_id]
    await broadcast({"type": "task_removed", "task_id": task_id})
    return {"deleted": task_id}


@app.get("/api/health")
async def api_health():
    return {"status": "ok", "ollama_model": llm_agent.model}


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

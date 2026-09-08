import asyncio
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from downloader import Downloader
from fastapi import WebSocket
from format_planner import FormatPlanner
from jellyfin import JellyfinClient
from monitor import MonitorStore
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

DEFAULT_MUSIC_FOLDER_KEY = "default_music_folder"

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

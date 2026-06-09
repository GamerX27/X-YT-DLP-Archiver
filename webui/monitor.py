"""
Playlist monitor — persistent storage and schedule helpers.

Each monitor stores a YouTube playlist URL and a schedule. The background
scheduler (in main.py) checks every 60 s and enqueues a download task
whenever a monitor is due. Because yt-dlp uses a per-playlist download
archive, only NEW videos are downloaded on subsequent runs.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MONITORS_FILE = Path("/app/monitors_data/monitors.json")

_SCHEDULE_DELTA: Dict[str, timedelta] = {
    "hourly": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
}


def _next_run(monitor: Dict[str, Any]) -> datetime:
    schedule = monitor.get("schedule", "daily")
    now = datetime.now()

    if schedule == "daily":
        time_str = monitor.get("schedule_time", "03:00")
        try:
            h, m = map(int, time_str.split(":"))
        except ValueError:
            h, m = 3, 0
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    delta = _SCHEDULE_DELTA.get(schedule, timedelta(days=1))
    return now + delta


class MonitorStore:
    def __init__(self, path: Path = MONITORS_FILE) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._monitors: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._monitors = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Could not load monitors file: %s", exc)

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps(self._monitors, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Could not save monitors file: %s", exc)

    def all(self) -> List[Dict[str, Any]]:
        return list(self._monitors.values())

    def get(self, monitor_id: str) -> Optional[Dict[str, Any]]:
        return self._monitors.get(monitor_id)

    def add(self, data: Dict[str, Any]) -> Dict[str, Any]:
        monitor_id = str(uuid.uuid4())
        monitor = {
            **data,
            "id": monitor_id,
            "created_at": datetime.now().isoformat(),
            "last_checked": None,
            "last_new_videos": 0,
            "status": "idle",
            "next_run": _next_run(data).isoformat(),
        }
        self._monitors[monitor_id] = monitor
        self._save()
        logger.info(
            "Monitor added: %s — %s",
            monitor.get("name", monitor_id),
            data.get("url", ""),
        )
        return monitor

    def update(self, monitor_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        if monitor_id not in self._monitors:
            return None
        self._monitors[monitor_id].update(kwargs)
        self._save()
        return self._monitors[monitor_id]

    def remove(self, monitor_id: str) -> bool:
        if monitor_id not in self._monitors:
            return False
        del self._monitors[monitor_id]
        self._save()
        return True

    def due(self) -> List[Dict[str, Any]]:
        now = datetime.now()
        result = []
        for m in self._monitors.values():
            if not m.get("enabled", True):
                continue
            next_run_str = m.get("next_run")
            if not next_run_str:
                result.append(m)
                continue
            try:
                if datetime.fromisoformat(next_run_str) <= now:
                    result.append(m)
            except ValueError:
                result.append(m)
        return result

    def mark_ran(self, monitor_id: str, new_videos: int = 0) -> None:
        if monitor_id not in self._monitors:
            return
        m = self._monitors[monitor_id]
        m["last_checked"] = datetime.now().isoformat()
        m["last_new_videos"] = new_videos
        m["status"] = "idle"
        m["next_run"] = _next_run(m).isoformat()
        self._save()

    def set_status(self, monitor_id: str, status: str) -> None:
        if monitor_id in self._monitors:
            self._monitors[monitor_id]["status"] = status
            self._save()

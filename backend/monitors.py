import logging
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import state
from downloader import normalize_playlist_url, sanitize_path

logger = logging.getLogger(__name__)


def active_task_for_url(url: str) -> Optional[Dict[str, Any]]:
    """Return an in-flight task already downloading ``url``, if any.

    Compares normalized playlist URLs so that e.g. a bare ``watch?v=…&list=…``
    and the full ``playlist?list=…`` form are treated as the same target.
    """
    target = normalize_playlist_url(url)
    for task in state.tasks.values():
        if task.get("status") not in state.ACTIVE_STATUSES:
            continue
        if normalize_playlist_url(task.get("url", "")) == target:
            return task
    return None


def monitor_for_url(url: str) -> Optional[Dict[str, Any]]:
    """Return a monitor watching the same playlist as ``url``, if any."""
    target = normalize_playlist_url(url)
    for monitor in state.monitor_store.all():
        if normalize_playlist_url(monitor.get("url", "")) == target:
            return monitor
    return None


def archive_count(monitor: Dict[str, Any]) -> int:
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
    state.monitor_store.set_status(monitor_id, "checking")
    await state.broadcast({"type": "monitors_update", "monitors": state.monitor_store.all()})

    playlist_count = 0
    try:
        meta = await state.downloader.probe_url(monitor["url"])
        playlist_count = meta.get("playlist_count") or 0
    except Exception as exc:
        logger.warning("Monitor %s probe failed: %s", monitor_id, exc)

    count = archive_count(monitor)

    state.monitor_store.update(
        monitor_id,
        playlist_count=playlist_count,
        archive_count=count,
    )
    await state.broadcast({"type": "monitors_update", "monitors": state.monitor_store.all()})

    if playlist_count > 0 and count >= playlist_count:
        logger.info(
            "Monitor %s up-to-date (%d/%d)", monitor_id, count, playlist_count
        )
        state.monitor_store.set_status(monitor_id, "up-to-date")
        state.monitor_store.mark_ran(monitor_id, new_videos=0)
        await state.broadcast({"type": "monitors_update", "monitors": state.monitor_store.all()})
        return

    # The download archive is only written as each video finishes, so during a
    # long playlist run archive_count stays below playlist_count for minutes.
    # Without this guard the 60 s scheduler would keep firing and enqueue a
    # duplicate task for the same playlist.
    existing = active_task_for_url(monitor["url"])
    if existing is not None:
        logger.info(
            "Monitor %s already has an active task %s for this playlist; skipping",
            monitor_id,
            existing["id"],
        )
        state.monitor_store.set_status(monitor_id, "downloading")
        await state.broadcast({"type": "monitors_update", "monitors": state.monitor_store.all()})
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
    state.tasks[task_id] = task
    await state.task_queue.put(task_id)
    await state.broadcast({"type": "task_update", "task": task})
    logger.info(
        "Monitor %s enqueued task %s (%d in archive, %d in playlist)",
        monitor_id,
        task_id,
        count,
        playlist_count,
    )

import logging
import shutil
import uuid
from pathlib import Path
from typing import Optional

import monitors
import state
from downloader import DOWNLOADS_DIR
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from models import DownloadRequest, ProbeRequest

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/probe")
async def api_probe(body: ProbeRequest):
    """Probe a URL and return which of the three resolution tiers are available."""
    is_music = "music.youtube.com" in body.url

    try:
        metadata = await state.downloader.probe_url(body.url)
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


@router.post("/api/download")
async def api_download(body: DownloadRequest):
    existing = monitors.active_task_for_url(body.url)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="This playlist is already downloading.",
        )

    # If this playlist is already watched by a monitor, route the download to
    # the monitor's Jellyfin destination so the same playlist can't diverge
    # into two folders/archives. The user's explicit form choices still win.
    monitor = monitors.monitor_for_url(body.url)
    is_music_link = "music.youtube.com" in body.url
    if monitor is None and is_music_link and not body.jellyfin_library_id:
        # No monitor and no explicit destination — fall back to the user's
        # configured default music folder, if one was set (see
        # /api/settings/default-music-folder). Mirrors the monitor-routing
        # logic above so calling /api/download directly still benefits from it.
        default_folder = state.settings_store.get(state.DEFAULT_MUSIC_FOLDER_KEY)
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
    state.tasks[task_id] = task
    await state.task_queue.put(task_id)
    await state.broadcast({"type": "task_update", "task": task})
    return task


@router.get("/api/tasks")
async def api_tasks():
    return list(state.tasks.values())


@router.get("/api/tasks/{task_id}/file")
async def api_download_task_file(task_id: str):
    """Serve a finished ad-hoc download's file for the browser to save."""
    task = state.tasks.get(task_id)
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


@router.post("/api/tasks/{task_id}/cancel")
async def api_cancel_task(task_id: str):
    """Signal a running download to stop and clean up its partial files."""
    if task_id not in state.tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    abort_event = state.task_abort_events.get(task_id)
    if abort_event is not None:
        abort_event.set()
    state.update_task(task_id, status="cancelling", status_text="Cancelling…")
    return {"cancelling": task_id}


@router.delete("/api/tasks/{task_id}")
async def api_delete_task(task_id: str):
    if task_id not in state.tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    task = state.tasks.pop(task_id)
    if task.get("browser_ready"):
        # This task's file lives only in the download cache, not a library —
        # clean it up now instead of waiting for the TTL sweep.
        shutil.rmtree(DOWNLOADS_DIR / task_id, ignore_errors=True)
    await state.broadcast({"type": "task_removed", "task_id": task_id})
    return {"deleted": task_id}

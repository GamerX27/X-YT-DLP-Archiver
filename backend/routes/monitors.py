import monitors
import state
from fastapi import APIRouter, HTTPException
from models import MonitorRequest

router = APIRouter()


@router.get("/api/monitors")
async def api_monitors():
    return state.monitor_store.all()


@router.post("/api/monitors")
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
            meta = await state.downloader.probe_url(body.url)
            name = meta.get("title") or meta.get("playlist_title") or body.url
            channel = meta.get("channel") or meta.get("uploader")
        except Exception:
            name = body.url
    monitor = state.monitor_store.add(
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
    await state.broadcast({"type": "monitors_update", "monitors": state.monitor_store.all()})
    return monitor


@router.delete("/api/monitors/{monitor_id}")
async def api_delete_monitor(monitor_id: str):
    if not state.monitor_store.remove(monitor_id):
        raise HTTPException(status_code=404, detail="Monitor not found")
    await state.broadcast({"type": "monitors_update", "monitors": state.monitor_store.all()})
    return {"deleted": monitor_id}


@router.post("/api/monitors/{monitor_id}/run")
async def api_run_monitor_now(monitor_id: str):
    monitor = state.monitor_store.get(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    await monitors.run_monitor(monitor)
    state.monitor_store.mark_ran(monitor_id)
    await state.broadcast({"type": "monitors_update", "monitors": state.monitor_store.all()})
    return {"queued": monitor_id}


@router.patch("/api/monitors/{monitor_id}")
async def api_update_monitor(monitor_id: str, body: dict):
    if "jellyfin_library_id" in body and not body["jellyfin_library_id"]:
        raise HTTPException(
            status_code=400, detail="A Jellyfin library is required for monitors"
        )
    monitor = state.monitor_store.update(monitor_id, **body)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    await state.broadcast({"type": "monitors_update", "monitors": state.monitor_store.all()})
    return monitor

import state
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/api/jellyfin/status")
async def api_jellyfin_status():
    return {"enabled": state.jellyfin_client.enabled}


@router.get("/api/jellyfin/libraries")
async def api_jellyfin_libraries():
    try:
        libs = await state.jellyfin_client.get_libraries()
        return {"libraries": libs}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Jellyfin error: {exc}")

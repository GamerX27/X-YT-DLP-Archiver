import disk
import state
from downloader import DOWNLOADS_DIR
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/health")
async def api_health():
    free_mb = disk.free_disk_mb(DOWNLOADS_DIR)
    return {
        "status": "ok",
        "disk_free_mb": free_mb,
        "disk_min_mb": state.MIN_FREE_DISK_MB,
        # True when there isn't enough room to safely start a new download.
        "disk_low": 0 <= free_mb < state.MIN_FREE_DISK_MB,
    }

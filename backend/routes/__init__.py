from fastapi import APIRouter

from . import downloads, health, jellyfin, monitors, pages, settings, ws

api_router = APIRouter()
api_router.include_router(pages.router)
api_router.include_router(downloads.router)
api_router.include_router(monitors.router)
api_router.include_router(jellyfin.router)
api_router.include_router(settings.router)
api_router.include_router(health.router)
api_router.include_router(ws.router)

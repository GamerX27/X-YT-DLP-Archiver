from pathlib import Path

import state
from fastapi import APIRouter
from models import DefaultMusicFolderRequest

router = APIRouter()


@router.get("/api/browse")
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


@router.get("/api/settings/default-music-folder")
async def api_get_default_music_folder():
    return state.settings_store.get(state.DEFAULT_MUSIC_FOLDER_KEY)


@router.post("/api/settings/default-music-folder")
async def api_set_default_music_folder(body: DefaultMusicFolderRequest):
    return state.settings_store.set(state.DEFAULT_MUSIC_FOLDER_KEY, body.model_dump())


@router.delete("/api/settings/default-music-folder")
async def api_clear_default_music_folder():
    state.settings_store.clear(state.DEFAULT_MUSIC_FOLDER_KEY)
    return {"cleared": True}

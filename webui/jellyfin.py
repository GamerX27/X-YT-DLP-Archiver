import logging
import os
from pathlib import Path
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

JELLYFIN_URL = os.getenv("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "")
# Path to Jellyfin media root as accessible inside THIS container.
# Jellyfin server may see the same data at a different path (e.g. /Media)
# while the container has it mounted at e.g. /mnt/NetworkShare/Media.
# We translate by matching the final directory component.
JELLYFIN_MEDIA_PATH = os.getenv("JELLYFIN_MEDIA_PATH", "").rstrip("/")


def _translate_path(jf_path: str) -> str:
    """
    Translate a Jellyfin-internal path to the host-accessible path.

    Example:
      jf_path            = /Media/YT
      JELLYFIN_MEDIA_PATH = /mnt/NetworkShare/Media
      result             = /mnt/NetworkShare/Media/YT

    Algorithm: find the last component of JELLYFIN_MEDIA_PATH inside
    jf_path, then graft JELLYFIN_MEDIA_PATH onto the remainder.
    Falls back to jf_path unchanged if no match is found.
    """
    if not JELLYFIN_MEDIA_PATH or not jf_path:
        return jf_path

    host_root = Path(JELLYFIN_MEDIA_PATH)
    anchor = host_root.name
    jf_parts = Path(jf_path).parts

    for i, part in enumerate(jf_parts):
        if part == anchor:
            remainder = jf_parts[i + 1 :]
            translated = (
                str(host_root.joinpath(*remainder)) if remainder else str(host_root)
            )
            logger.debug("Path translation: %s → %s", jf_path, translated)
            return translated

    logger.warning(
        "JELLYFIN_MEDIA_PATH anchor %r not found in Jellyfin path %r — using as-is",
        anchor,
        jf_path,
    )
    return jf_path


_TYPE_LABEL: Dict[str, str] = {
    "movies": "Movies",
    "tvshows": "TV Shows",
    "music": "Music",
    "books": "Books",
    "homevideos": "Home Videos",
    "photos": "Photos",
    "musicvideos": "Music Videos",
    "mixed": "Mixed",
}


class JellyfinClient:
    @property
    def enabled(self) -> bool:
        return bool(JELLYFIN_URL and JELLYFIN_API_KEY)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f'MediaBrowser Token="{JELLYFIN_API_KEY}"',
            "Accept": "application/json",
        }

    async def get_libraries(self) -> List[Dict[str, Any]]:
        """Return all virtual folders (libraries) from Jellyfin."""
        if not self.enabled:
            return []
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{JELLYFIN_URL}/Library/VirtualFolders",
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            out = []
            for lib in resp.json():
                locs = lib.get("Locations") or []
                if not locs or not lib.get("ItemId"):
                    continue
                col_type = (lib.get("CollectionType") or "mixed").lower()
                jf_path = locs[0]
                host_path = _translate_path(jf_path)
                out.append(
                    {
                        "id": lib["ItemId"],
                        "name": lib["Name"],
                        "type": col_type,
                        "type_label": _TYPE_LABEL.get(col_type, col_type.title()),
                        "path": host_path,  # container-accessible path
                        "jellyfin_path": jf_path,  # original Jellyfin path (for display)
                    }
                )
            return out

    async def refresh_library(self, library_id: str) -> None:
        """
        Trigger a Jellyfin library scan so newly added files are discovered.

        POST /Library/Refresh  — equivalent of “Scan All Libraries” in the UI.
        This is the correct call for file discovery; POST /Items/{id}/Refresh
        only refreshes metadata for items already in the database and will NOT
        pick up new files on disk.
        """
        if not self.enabled:
            return
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{JELLYFIN_URL}/Library/Refresh",
                headers=self._headers(),
                timeout=15,
            )
            # 204 No Content is the success response
            if resp.status_code not in (200, 204):
                resp.raise_for_status()
        logger.info(
            "Triggered Jellyfin library scan (POST /Library/Refresh) for library %s",
            library_id,
        )

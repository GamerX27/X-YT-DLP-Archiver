from typing import Optional

from pydantic import BaseModel


class DownloadRequest(BaseModel):
    url: str
    resolution_override: Optional[str] = None
    jellyfin_library_id: Optional[str] = None
    jellyfin_library_name: Optional[str] = None
    jellyfin_library_path: Optional[str] = None
    jellyfin_library_type: Optional[str] = None
    folder_override: Optional[str] = (
        None  # relative subfolder within the library (user-selected)
    )
    include_playlist_index: Optional[bool] = True


class ProbeRequest(BaseModel):
    url: str


class DefaultMusicFolderRequest(BaseModel):
    jellyfin_library_id: str
    jellyfin_library_name: Optional[str] = None
    jellyfin_library_path: Optional[str] = None
    jellyfin_library_type: Optional[str] = None
    folder_override: Optional[str] = None  # relative subfolder within the library


class MonitorRequest(BaseModel):
    url: str
    name: Optional[str] = None
    schedule: str = "daily"
    schedule_time: str = "03:00"
    schedule_day: Optional[int] = None  # 0=Monday…6=Sunday, used when schedule="weekly"
    resolution_override: Optional[str] = "1080p"
    jellyfin_library_id: Optional[str] = None
    jellyfin_library_name: Optional[str] = None
    jellyfin_library_path: Optional[str] = None
    jellyfin_library_type: Optional[str] = None
    folder_override: Optional[str] = None
    enabled: bool = True
    include_playlist_index: bool = True

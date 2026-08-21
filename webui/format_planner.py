import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_UNSAFE_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_name(name: str, max_len: int = 80) -> str:
    return _UNSAFE_PATH_CHARS.sub("_", str(name)).strip(". ")[:max_len]


class FormatPlanner:
    """
    Computes yt-dlp format strings, output templates, and destination folders
    purely from yt-dlp metadata — no external service or model involved.
    """

    def _summarize_metadata(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        formats = metadata.get("formats") or []

        heights = sorted(
            {
                f["height"]
                for f in formats
                if isinstance(f.get("height"), int) and f["height"] > 0
            },
            reverse=True,
        )[:10]

        seen_codecs: set[str] = set()
        for f in formats:
            vc = f.get("vcodec")
            if vc and vc != "none":
                seen_codecs.add(vc.split(".")[0])
        codecs = sorted(seen_codecs)[:8]

        # Channel name — equivalent of: yt-dlp --print "%(channel)s" URL
        # For playlists, the entries carry the real video channel (e.g. "Sonic Discord Server")
        # while the top-level may hold the playlist owner (e.g. "X27").
        # Always prefer entry consensus so the folder matches what --print returns.
        entries = metadata.get("entries") or []
        entry_channels = [
            e.get("channel") or e.get("uploader")
            for e in entries
            if e.get("channel") or e.get("uploader")
        ]
        if entry_channels:
            # All videos share the same channel name → that is the authoritative value
            channel = max(set(entry_channels), key=entry_channels.count)
        else:
            channel = metadata.get("channel") or metadata.get("uploader")

        is_playlist = metadata.get("_type") == "playlist"

        # Playlist title: equivalent of:
        # yt-dlp --skip-download --no-warnings --print playlist_title "URL"
        # (prints the same value for every video — we de-dup by just reading the field)
        playlist_title = metadata.get("playlist_title") or (
            metadata.get("title") if is_playlist else None
        )

        # Upload year — used by Jellyfin Season grouping.
        # For playlists the top-level date is absent; fall back to first entry.
        raw_date = metadata.get("upload_date") or ""
        if not raw_date and entries:
            raw_date = entries[0].get("upload_date", "") or ""
        upload_year = raw_date[:4] if len(raw_date) >= 4 else ""

        return {
            "title": metadata.get("title"),
            "channel": channel,
            "is_playlist": is_playlist,
            "playlist_title": playlist_title,
            "playlist_count": metadata.get("playlist_count"),
            "available_heights": heights,
            "available_codecs": codecs,
            "max_height": heights[0] if heights else 0,
            "extractor": metadata.get("extractor"),
            "was_live": metadata.get("was_live"),
            "upload_year": upload_year,
            # Music metadata (populated by YouTube Music / other music extractors)
            "artist": metadata.get("artist") or metadata.get("creator"),
            "album": metadata.get("album"),
        }

    def _folder_from_summary(self, summary: Dict[str, Any]) -> tuple[str, str, str]:
        """Compute (channel_name, playlist_name, folder_path) from metadata."""
        channel = _sanitize_name(
            summary.get("channel") or summary.get("title") or "Unknown"
        )
        is_playlist = summary.get("is_playlist", False)
        raw_playlist = summary.get("playlist_title") if is_playlist else None
        playlist = _sanitize_name(raw_playlist) if raw_playlist else None

        folder = f"{channel}/{playlist}" if playlist else channel
        return channel, playlist or "", folder

    def _folder_for_jellyfin(
        self,
        summary: Dict[str, Any],
        library_type: str,
        include_playlist_index: bool = True,
    ) -> tuple[str, str, str, str]:
        """
        Return (channel, playlist, folder_path, output_template) for Jellyfin mode.
        Computed entirely from metadata.
        """
        channel = _sanitize_name(
            summary.get("channel") or summary.get("title") or "Unknown"
        )
        is_playlist = summary.get("is_playlist", False)
        raw_playlist = summary.get("playlist_title") if is_playlist else None
        playlist = _sanitize_name(raw_playlist) if raw_playlist else None
        year = summary.get("upload_year", "")

        lt = (library_type or "").lower()

        if lt == "movies":
            folder = channel
            template = "%(title)s (%(upload_date>%Y)s).%(ext)s"

        elif lt == "tvshows":
            if is_playlist and playlist:
                folder = f"{channel}/{playlist}"
                template = (
                    "%(playlist_index)02d - %(title)s [%(id)s].%(ext)s"
                    if include_playlist_index
                    else "%(title)s [%(id)s].%(ext)s"
                )
            elif year:
                folder = f"{channel}/Season {year}"
                template = "%(upload_date>%Y-%m-%d)s - %(title)s [%(id)s].%(ext)s"
            else:
                folder = channel
                template = "%(upload_date>%Y-%m-%d)s - %(title)s [%(id)s].%(ext)s"

        elif lt == "music":
            # For music libraries use the actual artist name as the top-level folder.
            # YouTube Music provides an `artist` field; fall back to channel.
            artist = _sanitize_name(
                summary.get("artist")
                or summary.get("channel")
                or summary.get("title")
                or "Unknown"
            )
            if is_playlist and playlist:
                folder = f"{artist}/{playlist}"
                template = (
                    "%(playlist_index)02d - %(title)s.%(ext)s"
                    if include_playlist_index
                    else "%(title)s.%(ext)s"
                )
            else:
                folder = artist
                template = "%(title)s.%(ext)s"
            return artist, playlist or "", folder, template

        else:  # homevideos, mixed, musicvideos, photos, …
            if is_playlist and playlist:
                folder = f"{channel}/{playlist}"
                template = (
                    "%(playlist_index)02d - %(title)s.%(ext)s"
                    if include_playlist_index
                    else "%(title)s.%(ext)s"
                )
            else:
                folder = channel
                template = "%(title)s.%(ext)s"

        return channel, playlist or "", folder, template

    def _format_from_resolution(
        self, resolution_override: Optional[str], summary: Dict[str, Any]
    ) -> tuple[str, Dict[str, Any]]:
        """Rule-based yt-dlp format string + extra_opts for the chosen resolution."""
        if resolution_override == "audio":
            # Equivalent to: yt-dlp -x --audio-format mp3 --audio-quality 0
            #                        --embed-thumbnail --add-metadata -f bestaudio/best
            return "bestaudio/best", {
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "0",  # VBR best (~320 kbps)
                    }
                ],
                "embedthumbnail": True,
                "addmetadata": True,
            }
        if resolution_override == "720p":
            return (
                "bestvideo[vcodec^=avc][height<=720]+bestaudio[ext=m4a]"
                "/bestvideo[vcodec^=avc][height<=720]+bestaudio"
                "/bestvideo[height<=720]+bestaudio/best[height<=720]"
            ), {}
        if resolution_override == "1080p":
            return (
                "bestvideo[vcodec^=avc][height<=1080]+bestaudio[ext=m4a]"
                "/bestvideo[vcodec^=avc][height<=1080]+bestaudio"
                "/bestvideo[height<=1080]+bestaudio/best[height<=1080]"
            ), {}
        if resolution_override == "1440p":
            return (
                "bestvideo[height<=1440]+bestaudio[ext=m4a]"
                "/bestvideo[height<=1440]+bestaudio/bestvideo+bestaudio/best"
            ), {}
        if resolution_override in ("2160p", "4k"):
            return (
                "bestvideo[height<=2160]+bestaudio[ext=m4a]"
                "/bestvideo[height<=2160]+bestaudio/bestvideo+bestaudio/best"
            ), {}
        return "bestvideo+bestaudio/best", {}

    async def analyze(
        self,
        metadata: Dict[str, Any],
        resolution_override: Optional[str] = None,
        jellyfin_library_type: Optional[str] = None,
        include_playlist_index: bool = True,
    ) -> Dict[str, Any]:
        summary = self._summarize_metadata(metadata)

        # Folder + template are always computed straight from metadata.
        is_playlist = summary.get("is_playlist", False)
        if jellyfin_library_type:
            channel, playlist, folder, output_template = self._folder_for_jellyfin(
                summary, jellyfin_library_type, include_playlist_index
            )
        else:
            channel, playlist, folder = self._folder_from_summary(summary)
            if is_playlist:
                output_template = (
                    "%(playlist_index)02d - %(title)s.%(ext)s"
                    if include_playlist_index
                    else "%(title)s.%(ext)s"
                )
            else:
                output_template = "%(title)s.%(ext)s"

        logger.info(
            "Task analysis — channel=%r  playlist=%r  folder=%r  "
            "resolution=%s  jellyfin_type=%s",
            channel,
            playlist or None,
            folder,
            resolution_override,
            jellyfin_library_type or "—",
        )

        fmt, extra = self._format_from_resolution(resolution_override, summary)

        return {
            "channel_name": channel,
            "content_type": "playlist" if is_playlist else "video",
            "playlist_name": playlist or None,
            "folder_path": folder,
            "format_string": fmt,
            "output_template": output_template,
            "extra_opts": extra,
            "reasoning": "Rule-based plan",
        }

    async def order_playlist_episodes(self, episodes: list) -> list:
        """
        Given [{filename, playlist_index, upload_date}] sorted by playlist_index,
        return [{filename, new_date}] with dates corrected to be strictly ascending
        so Jellyfin displays episodes in playlist order.
        """
        result = []
        prev_date = None
        for ep in episodes:
            try:
                d = datetime.strptime(ep["upload_date"], "%Y-%m-%d").date()
            except (ValueError, KeyError):
                d = (
                    (prev_date + timedelta(days=1))
                    if prev_date
                    else datetime.today().date()
                )
            if prev_date is not None and d <= prev_date:
                d = prev_date + timedelta(days=1)
            prev_date = d
            result.append(
                {"filename": ep["filename"], "new_date": d.strftime("%Y-%m-%d")}
            )
        return result

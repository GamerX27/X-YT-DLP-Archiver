import asyncio
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import yt_dlp
from yt_dlp.postprocessor.common import PostProcessor

logger = logging.getLogger(__name__)
DOWNLOADS_DIR = Path(os.getenv("DOWNLOADS_DIR", "/app/tmp_downloads"))

_NETWORK_RETRY_MAX = 10
_NETWORK_RETRY_DELAY = 10  # seconds between retries

# Substrings that indicate a transient network failure worth retrying
_NETWORK_ERROR_HINTS = (
    "Network is unreachable",
    "Failed to establish a new connection",
    "Connection refused",
    "Connection reset",
    "timed out",
    "Temporary failure in name resolution",
    "Name or service not known",
    "more expected",  # truncated HTTP response
    "RemoteDisconnected",  # server closed connection mid-stream
)


def _is_network_error(exc: Exception) -> bool:
    msg = str(exc)
    return any(hint in msg for hint in _NETWORK_ERROR_HINTS)


class DownloadCancelled(Exception):
    """Raised from the yt-dlp progress hook when an abort_event is set."""


# Run as root inside the container but chown media files to the host user
# so they can be moved/deleted on the host without sudo.
_PUID = int(os.getenv("PUID", "1000"))
_PGID = int(os.getenv("PGID", "1000"))


def sanitize_path(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(". ")[:100]


def sanitize_info(info: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of info with non-serialisable values removed."""
    clean: Dict[str, Any] = {}
    for k, v in info.items():
        if isinstance(v, (str, int, float, bool, type(None))):
            clean[k] = v
        elif isinstance(v, list):
            clean[k] = v
        elif isinstance(v, dict):
            clean[k] = v
    return clean


def _upload_date_to_metric(upload_date: Optional[str]) -> str:
    """
    Convert yt-dlp's YYYYMMDD → DD.MM.YYYY (e.g. '20240529' → '29.05.2024').
    Also accepts YYYY-MM-DD (ISO) as a fallback input format.
    Returns '' if the value is absent or unrecognised.
    """
    if not upload_date:
        return ""
    s = upload_date.strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[6:]}.{s[4:6]}.{s[:4]}"
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        yyyy, mm, dd = s.split("-")
        return f"{dd}.{mm}.{yyyy}"
    return ""


def _timestamp_to_metric(ts: Any) -> str:
    """Convert a Unix timestamp (int/float) → DD.MM.YYYY."""
    if not ts:
        return ""
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%d.%m.%Y")
    except Exception:
        return ""


def _upload_date_as_datetime(upload_date: Optional[str]) -> Optional[Any]:
    """Return a datetime for the YYYYMMDD string, or None."""
    if not upload_date or len(upload_date) != 8 or not upload_date.isdigit():
        return None
    try:
        from datetime import datetime

        return datetime(
            int(upload_date[:4]), int(upload_date[4:6]), int(upload_date[6:])
        )
    except ValueError:
        return None


class _EmbedPP(PostProcessor):
    """
    Custom postprocessor: embeds thumbnail and metadata via mutagen.

    Pipeline:
      1. WriteThumbnailPP (added via writethumbnail=True) downloads the thumbnail.
      2. For audio: FFmpegExtractAudio has already produced the .mp3 by this point.
      3. WebP thumbnails are converted to JPEG via ffmpeg before embedding.
      4. MP4/M4A  → mutagen MP4: cover art, title, artist, album, date, description.
         MP3       → mutagen ID3: APIC cover, TIT2 title, TPE1/TPE2 artist, TALB album.
      5. The standalone thumbnail file is deleted — only the embedded copy remains.
    """

    def run(self, info: Dict[str, Any]):  # type: ignore[override]
        filepath = info.get("filepath")
        if not filepath:
            return [], info

        path = Path(filepath)

        # Convert WebP thumbnail → JPEG (needed for both MP3 and MP4)
        def _prep_thumb() -> Optional[Path]:
            t = self._find_thumbnail(info, path)
            if t and t.suffix.lower() == ".webp":
                converted = self._webp_to_jpg(t)
                try:
                    t.unlink(missing_ok=True)
                except OSError:
                    pass
                return converted
            return t

        if path.suffix.lower() == ".mp3":
            thumb = _prep_thumb()
            self._id3_embed(path, thumb, info)
            if thumb and thumb.exists():
                try:
                    thumb.unlink(missing_ok=True)
                except OSError:
                    pass
            return [], info

        if path.suffix.lower() not in (".mp4", ".m4a", ".m4v", ".mov"):
            return [], info

        thumb = _prep_thumb()
        self._mutagen_embed(path, thumb, info)
        if thumb and thumb.exists():
            try:
                thumb.unlink(missing_ok=True)
            except OSError:
                pass
        return [], info

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _find_thumbnail(info: Dict[str, Any], video_path: Path) -> Optional[Path]:
        """Locate the thumbnail WriteThumbnailPP wrote to disk."""
        # WriteThumbnailPP sets 'filepath' on the thumbnail dict entry it writes
        for thumb in reversed(info.get("thumbnails") or []):
            fp = thumb.get("filepath")
            if fp and Path(fp).exists():
                return Path(fp)
        # Fallback: same stem, common image extensions
        for ext in (".webp", ".jpg", ".jpeg", ".png"):
            candidate = video_path.with_suffix(ext)
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def _webp_to_jpg(src: Path) -> Optional[Path]:
        dst = src.with_suffix(".jpg")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), str(dst)],
                check=True,
                capture_output=True,
            )
            return dst
        except subprocess.CalledProcessError as exc:
            logger.warning("WebP→JPG conversion failed: %s", exc.stderr.decode())
            return None

    @staticmethod
    def _mutagen_embed(path: Path, thumb: Optional[Path], info: Dict[str, Any]) -> None:
        try:
            from mutagen.mp4 import MP4, MP4Cover
        except ImportError:
            logger.error("mutagen is not installed — skipping metadata embed")
            return

        try:
            video = MP4(str(path))
            if video.tags is None:
                video.add_tags()
            tags = video.tags

            # Cover art
            if thumb and thumb.exists():
                img_fmt = (
                    MP4Cover.FORMAT_JPEG
                    if thumb.suffix.lower() in (".jpg", ".jpeg")
                    else MP4Cover.FORMAT_PNG
                )
                tags["covr"] = [MP4Cover(thumb.read_bytes(), imageformat=img_fmt)]

            # Title
            if info.get("title"):
                tags["\xa9nam"] = [info["title"]]

            # Artist — prefer the music `artist` field (YouTube Music provides it),
            # fall back to channel/uploader for regular videos.
            artist = (
                info.get("artist")
                or info.get("creator")
                or info.get("channel")
                or info.get("uploader")
            )
            if artist:
                tags["\xa9ART"] = [artist]
                tags["aART"] = [artist]

            # Album (populated by YouTube Music)
            album = info.get("album") or info.get("playlist_title")
            if album:
                tags["\xa9alb"] = [album]

            # Upload date
            # yt-dlp stores YYYYMMDD in info['upload_date'] (may be None even if
            # the key exists).  Fall back to the Unix timestamp when absent.
            raw_date: Optional[str] = info.get("upload_date") or None
            date_metric = _upload_date_to_metric(raw_date)
            if not date_metric:
                date_metric = _timestamp_to_metric(info.get("timestamp"))
            logger.info(
                "upload_date raw=%r  →  metric=%r  (file: %s)",
                raw_date,
                date_metric,
                path.name,
            )
            if date_metric:
                tags["\xa9day"] = [date_metric]

            # Description
            description = info.get("description") or ""
            if description:
                tags["\xa9cmt"] = [description[:2000]]

            video.save()
            logger.info("Embedded metadata+thumbnail → %s", path.name)

            # Set the file's modification time to the upload date so file
            # managers and media servers show the correct "creation date".
            dt = _upload_date_as_datetime(raw_date)
            if dt is not None:
                import os as _os

                ts = dt.timestamp()
                _os.utime(str(path), (ts, ts))
                logger.info("Set mtime of %s → %s", path.name, dt.date())

        except Exception as exc:
            logger.warning("mutagen embed failed for %s: %s", path.name, exc)

    @staticmethod
    def _id3_embed(path: Path, thumb: Optional[Path], info: Dict[str, Any]) -> None:
        """Embed cover art + ID3 tags into an MP3 file using mutagen."""
        try:
            from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TPE2
            from mutagen.mp3 import MP3
        except ImportError:
            logger.error("mutagen is not installed — skipping MP3 metadata embed")
            return

        try:
            audio = MP3(str(path), ID3=ID3)
            if audio.tags is None:
                audio.add_tags()

            # Cover art
            if thumb and thumb.exists():
                mime = (
                    "image/jpeg"
                    if thumb.suffix.lower() in (".jpg", ".jpeg")
                    else "image/png"
                )
                audio.tags.add(
                    APIC(
                        encoding=3,
                        mime=mime,
                        type=3,  # Cover (front)
                        desc="Cover",
                        data=thumb.read_bytes(),
                    )
                )

            # Title
            if info.get("title"):
                audio.tags.add(TIT2(encoding=3, text=[info["title"]]))

            # Artist — prefer music metadata, fall back to channel
            artist = (
                info.get("artist")
                or info.get("creator")
                or info.get("channel")
                or info.get("uploader")
            )
            if artist:
                audio.tags.add(TPE1(encoding=3, text=[artist]))  # lead artist
                audio.tags.add(TPE2(encoding=3, text=[artist]))  # album artist

            # Album
            album = info.get("album") or info.get("playlist_title")
            if album:
                audio.tags.add(TALB(encoding=3, text=[album]))

            audio.save()
            logger.info("Embedded MP3 metadata+thumbnail → %s", path.name)

        except Exception as exc:
            logger.warning("ID3 embed failed for %s: %s", path.name, exc)


class Downloader:
    # ── Probe ────────────────────────────────────────────────────────────────

    async def probe_url(self, url: str) -> Dict[str, Any]:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "playlist_items": "1:5",
        }
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, self._probe_sync, url, opts)
        return sanitize_info(info) if info else {}

    @staticmethod
    def _probe_sync(url: str, opts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                return ydl.extract_info(url, download=False)
            except Exception as exc:
                logger.warning("probe failed for %s: %s", url, exc)
                return None

    # ── Download ─────────────────────────────────────────────────────────────

    async def download(
        self,
        url: str,
        format_string: str,
        output_template: str,
        extra_opts: Dict[str, Any],
        folder_path: str,
        task_id: str,
        progress_cb: Callable[
            [float, str, str, str, Optional[int], Optional[int]], Any
        ],
        abort_event: Optional[threading.Event] = None,
        is_audio: bool = False,
    ) -> Path:
        dest = DOWNLOADS_DIR / task_id
        dest.mkdir(parents=True, exist_ok=True)

        safe_folder = "/".join(
            sanitize_path(p) for p in folder_path.split("/") if p.strip()
        )
        full_output = str(dest / safe_folder / output_template)

        loop = asyncio.get_event_loop()

        def progress_hook(d: Dict[str, Any]) -> None:
            if abort_event is not None and abort_event.is_set():
                raise DownloadCancelled
            status = d.get("status")
            info_dict = d.get("info_dict") or {}
            playlist_index = info_dict.get("playlist_index")
            playlist_count = info_dict.get("playlist_count") or info_dict.get(
                "n_entries"
            )

            if status == "already_downloaded":
                # Video was skipped because it is in the download archive
                title = info_dict.get("title") or d.get("filename", "")
                asyncio.run_coroutine_threadsafe(
                    progress_cb(
                        100,
                        "—",
                        "—",
                        f"[skipped] {title}",
                        playlist_index,
                        playlist_count,
                    ),
                    loop,
                )
                return

            if status != "downloading":
                return

            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100) if total > 0 else 0.0
            speed_raw = d.get("speed")
            speed = f"{speed_raw / 1024:.1f} KB/s" if speed_raw else "—"
            eta_raw = d.get("eta")
            eta = f"{eta_raw}s" if eta_raw is not None else "—"
            filename = d.get("filename", "")
            asyncio.run_coroutine_threadsafe(
                progress_cb(
                    percent, speed, eta, filename, playlist_index, playlist_count
                ),
                loop,
            )

        opts: Dict[str, Any] = {
            "format": format_string,
            "outtmpl": full_output,
            "progress_hooks": [progress_hook],
            "noprogress": True,
            **extra_opts,
            # Hard overrides — LLM cannot change these.
            "keepvideo": False,
        }
        # Strip any playlist-capping options the LLM may have hallucinated.
        # Small models like llama3.2:1b sometimes add max_downloads or
        # playlistend which silently truncates long playlists.
        for _cap_key in (
            "max_downloads",
            "playlistend",
            "playlist_items",
            "playliststart",
            "playlistend",
        ):
            opts.pop(_cap_key, None)
        # Always write the thumbnail to disk so _EmbedPP can pick it up.
        # For video it is embedded as MP4 cover art; for audio as ID3 APIC.
        opts["writethumbnail"] = True
        if not is_audio:
            opts["merge_output_format"] = "mp4"

        await loop.run_in_executor(None, self._download_sync, url, opts)
        self._cleanup_temp_format_files(dest)
        return dest

    @staticmethod
    def _download_sync(url: str, opts: Dict[str, Any]) -> None:
        def _run(o: Dict[str, Any]) -> None:
            with yt_dlp.YoutubeDL(o) as ydl:
                # _EmbedPP runs after WriteThumbnailPP and (for audio)
                # after FFmpegExtractAudio, embedding cover art + tags via
                # mutagen for both MP4 and MP3 files.
                ydl.add_post_processor(_EmbedPP(ydl))
                ydl.download([url])

        def _run_with_retries(o: Dict[str, Any]) -> None:
            for attempt in range(1, _NETWORK_RETRY_MAX + 1):
                try:
                    _run(o)
                    return
                except yt_dlp.utils.DownloadError as exc:
                    if _is_network_error(exc) and attempt < _NETWORK_RETRY_MAX:
                        logger.warning(
                            "Network error on attempt %d/%d, retrying in %ds: %s",
                            attempt,
                            _NETWORK_RETRY_MAX,
                            _NETWORK_RETRY_DELAY,
                            exc,
                        )
                        time.sleep(_NETWORK_RETRY_DELAY)
                    else:
                        raise

        try:
            _run_with_retries(opts)
        except yt_dlp.utils.DownloadError as exc:
            if "Requested format is not available" in str(exc):
                logger.warning(
                    "Format '%s' unavailable, retrying with bestvideo+bestaudio/best",
                    opts.get("format"),
                )
                _run_with_retries({**opts, "format": "bestvideo+bestaudio/best"})
            else:
                raise

    @staticmethod
    def _cleanup_temp_format_files(directory: Path) -> None:
        # Remove intermediate muxer files (e.g. filename.f136.mp4)
        fmt_pattern = re.compile(r"\.f\d{2,4}\.[a-zA-Z0-9]{2,4}$")
        for f in directory.rglob("*"):
            if f.is_file() and fmt_pattern.search(f.name):
                logger.info("Removed temp format file: %s", f.name)
                f.unlink(missing_ok=True)
        # Remove leftover image files — these are playlist-level thumbnails
        # that yt-dlp writes but _EmbedPP never embeds (no matching video).
        # Per-video thumbnails are already deleted by _EmbedPP after embedding.
        thumb_exts = {".jpg", ".jpeg", ".png", ".webp"}
        for f in directory.rglob("*"):
            if f.is_file() and f.suffix.lower() in thumb_exts:
                logger.info("Removed leftover thumbnail: %s", f.name)
                f.unlink(missing_ok=True)

    # ── Move ─────────────────────────────────────────────────────────────────

    async def move_to_media(
        self, task_dir: Path, folder_path: str, media_dir: str
    ) -> Path:
        safe_folder = "/".join(
            sanitize_path(p) for p in folder_path.split("/") if p.strip()
        )
        src = task_dir / safe_folder
        dest = Path(media_dir) / safe_folder
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._move_sync, src, dest, task_dir)
        return result

    @staticmethod
    def _move_sync(src: Path, dest: Path, task_dir: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            if dest.exists():
                for item in src.iterdir():
                    shutil.move(str(item), str(dest / item.name))
            else:
                shutil.move(str(src), str(dest))

        # Fix ownership so the host user can move/delete files without sudo
        try:
            for path in dest.rglob("*"):
                os.chown(path, _PUID, _PGID)
                path.chmod(0o755 if path.is_dir() else 0o644)
            os.chown(dest, _PUID, _PGID)
            dest.chmod(0o755)
        except Exception as exc:
            logger.warning("chown failed (non-fatal): %s", exc)

        shutil.rmtree(task_dir, ignore_errors=True)
        return dest

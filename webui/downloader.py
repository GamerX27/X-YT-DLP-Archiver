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
from urllib.parse import parse_qs, urlencode, urlparse

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

_YOUTUBE_PLAYER_CLIENT = [
    c.strip() for c in os.getenv("YOUTUBE_PLAYER_CLIENT", "android").split(",") if c.strip()
]


def _base_extractor_args() -> Dict[str, Any]:
    if not _YOUTUBE_PLAYER_CLIENT:
        return {}
    return {"youtube": {"player_client": _YOUTUBE_PLAYER_CLIENT}}


# YouTube "list" IDs that are dynamically generated mixes/radios. These have
# no standalone playlist page, so they must NOT be rewritten to /playlist —
# doing so would make yt-dlp fail to resolve them.
_NON_BROWSABLE_LIST_PREFIXES = ("RD", "UL", "MM")


def normalize_playlist_url(url: str) -> str:
    """Rewrite a YouTube *watch* URL that carries a ``list=`` parameter into the
    canonical ``/playlist?list=<id>`` form.

    A ``watch?v=...&list=...`` URL makes yt-dlp extract the playlist from the
    *watch page's* side panel, which YouTube hard-caps at ~100 entries for
    anyone who is not the playlist owner. That is why a 181-video playlist
    silently stops at 100 — and why ``lazy_playlist=False`` cannot help: the
    watch page never exposes the remaining continuation pages at all.

    The dedicated playlist tab (``/playlist?list=<id>``) returns *every* entry,
    so for real (browsable) playlists we redirect to it. Mixes/radios (``RD``…,
    which have no static page) are left untouched.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    host = (parsed.hostname or "").lower()
    if not (host.endswith("youtube.com") or host == "youtu.be"):
        return url

    list_ids = parse_qs(parsed.query).get("list")
    if not list_ids:
        return url
    list_id = list_ids[0]

    # Already a bare playlist URL (or a mix/radio that has no static page).
    if parsed.path.rstrip("/").endswith("/playlist"):
        return url
    if any(list_id.startswith(prefix) for prefix in _NON_BROWSABLE_LIST_PREFIXES):
        return url

    new_host = (
        "music.youtube.com" if host.endswith("music.youtube.com") else "www.youtube.com"
    )
    new_url = f"https://{new_host}/playlist?{urlencode({'list': list_id})}"
    logger.info(
        "Normalized watch+list URL to full playlist URL so all entries are "
        "fetched (watch-page panels are capped at ~100): %s\u2002\u2192\u2002%s",
        url,
        new_url,
    )
    return new_url


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


def _upload_date_to_iso(upload_date: Optional[str]) -> str:
    """yt-dlp YYYYMMDD (or already-ISO) → 'YYYY-MM-DD'. '' if unrecognised.

    ISO is what Jellyfin parses reliably; the older DD.MM.YYYY form was parsed
    inconsistently, which is why some videos showed a year and others didn't.
    """
    if not upload_date:
        return ""
    s = upload_date.strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    return ""


def _timestamp_to_iso(ts: Any) -> str:
    """Unix timestamp (int/float) → 'YYYY-MM-DD'. '' if absent/invalid."""
    if not ts:
        return ""
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""


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

            if thumb and thumb.exists():
                img_fmt = (
                    MP4Cover.FORMAT_JPEG
                    if thumb.suffix.lower() in (".jpg", ".jpeg")
                    else MP4Cover.FORMAT_PNG
                )
                tags["covr"] = [MP4Cover(thumb.read_bytes(), imageformat=img_fmt)]

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

            # yt-dlp stores YYYYMMDD in info['upload_date'] (may be None even if
            # the key exists).  Fall back to the Unix timestamp when absent.
            # Embed as ISO YYYY-MM-DD so Jellyfin parses the year reliably, and
            # keep the file mtime in sync with the SAME date below.
            raw_date: Optional[str] = info.get("upload_date") or None
            date_iso = _upload_date_to_iso(raw_date) or _timestamp_to_iso(
                info.get("timestamp")
            )
            logger.info(
                "upload_date raw=%r  →  iso=%r  (file: %s)",
                raw_date,
                date_iso,
                path.name,
            )
            if date_iso:
                tags["\xa9day"] = [date_iso]

            description = info.get("description") or ""
            if description:
                tags["\xa9cmt"] = [description[:2000]]

            video.save()
            logger.info("Embedded metadata+thumbnail → %s", path.name)

            # Set the file's modification time to the SAME date that was
            # embedded above, so file managers and Jellyfin show a consistent
            # date. Uses the timestamp fallback too (the old code only set the
            # mtime when a YYYYMMDD upload_date existed, so videos exposing only
            # a timestamp kept their download date — a source of wrong years).
            if date_iso:
                import os as _os
                from datetime import datetime as _dt

                try:
                    ts = _dt.strptime(date_iso, "%Y-%m-%d").timestamp()
                    _os.utime(str(path), (ts, ts))
                    logger.info("Set mtime of %s → %s", path.name, date_iso)
                except ValueError:
                    pass

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

            album = info.get("album") or info.get("playlist_title")
            if album:
                audio.tags.add(TALB(encoding=3, text=[album]))

            audio.save()
            logger.info("Embedded MP3 metadata+thumbnail → %s", path.name)

        except Exception as exc:
            logger.warning("ID3 embed failed for %s: %s", path.name, exc)


class Downloader:
    @staticmethod
    def set_video_date(path: Path, iso_date: str) -> None:
        """Set both the embedded date tag and the file mtime of an MP4 to
        ``iso_date`` (YYYY-MM-DD).

        Used by the Jellyfin reorder step so the date Jellyfin displays and the
        date it sorts by always agree — previously the reorder rewrote only the
        mtime, leaving the embedded tag at a different value. No-op on failure.
        """
        try:
            from datetime import datetime

            ts = datetime.strptime(iso_date, "%Y-%m-%d").timestamp()
        except ValueError:
            return
        try:
            from mutagen.mp4 import MP4

            video = MP4(str(path))
            if video.tags is None:
                video.add_tags()
            video.tags["\xa9day"] = [iso_date]
            video.save()
        except Exception as exc:
            # Non-MP4 containers (mkv/webm) can't hold this tag — the mtime
            # below still keeps ordering correct.
            logger.warning("Could not update embedded date for %s: %s", path.name, exc)
        try:
            os.utime(str(path), (ts, ts))
        except OSError as exc:
            logger.warning("Could not set mtime for %s: %s", path.name, exc)

    async def probe_url(self, url: str) -> Dict[str, Any]:
        # Use the full playlist tab instead of a capped watch-page panel so the
        # reported playlist_count matches what will actually be downloaded.
        url = normalize_playlist_url(url)
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "playlist_items": "1:5",
            "extractor_args": _base_extractor_args(),
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
        # A watch?v=...&list=... URL only exposes the watch-page playlist panel
        # (capped at ~100 entries). Rewrite it to the full playlist tab so every
        # entry is downloaded.
        url = normalize_playlist_url(url)

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
            # Placed after the extra_opts spread so it can't be overridden.
            "keepvideo": False,
        }
        # extra_opts must never cap a playlist — strip these if present.
        for _cap_key in (
            "max_downloads",
            "playlistend",
            "playliststart",
        ):
            opts.pop(_cap_key, None)
        # Force yt-dlp to fetch ALL playlist pages before processing.
        # With lazy_playlist=True (default in newer yt-dlp), the YouTube
        # extractor only returns the first API page (100 items) as a list.
        # Setting lazy_playlist=False loads all pages eagerly upfront.
        opts["lazy_playlist"] = False
        opts["extractor_args"] = _base_extractor_args()
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
    def _read_tags(path: Path) -> tuple[Optional[str], Optional[str]]:
        """Best-effort read of the embedded (title, artist) tags from an
        already-tagged media file, used to tell whether two files sharing a
        filename are actually the same track or genuinely different songs.
        """
        suffix = path.suffix.lower()
        try:
            if suffix == ".mp3":
                from mutagen.id3 import ID3

                tags = ID3(str(path))
                title_frame = tags.get("TIT2")
                artist_frame = tags.get("TPE1")
                title = str(title_frame.text[0]) if title_frame and title_frame.text else None
                artist = str(artist_frame.text[0]) if artist_frame and artist_frame.text else None
                return title, artist
            elif suffix in (".mp4", ".m4a", ".m4v", ".mov"):
                from mutagen.mp4 import MP4

                tags = MP4(str(path)).tags
                title = str(tags["\xa9nam"][0]) if tags and tags.get("\xa9nam") else None
                artist = str(tags["\xa9ART"][0]) if tags and tags.get("\xa9ART") else None
                return title, artist
        except Exception as exc:
            logger.debug("Could not read tags from %s: %s", path.name, exc)
        return None, None

    @classmethod
    def _read_artist_tag(cls, path: Path) -> Optional[str]:
        return cls._read_tags(path)[1]

    @classmethod
    def _is_same_track(cls, target: Path, item: Path) -> bool:
        """Decide whether ``target`` (existing file) and ``item`` (incoming
        file) are the same track, so it is safe to overwrite ``target``
        instead of treating this as a genuine filename collision.
        """
        target_title, target_artist = cls._read_tags(target)
        item_title, item_artist = cls._read_tags(item)
        if target_title is not None or item_title is not None:
            # At least one file has readable tags — trust them over size,
            # since two different songs can coincidentally share a byte size.
            norm = lambda s: (s or "").strip().casefold()
            return norm(target_title) == norm(item_title) and norm(
                target_artist
            ) == norm(item_artist)
        # Neither file has readable tags (e.g. non-audio/video file such as
        # a leftover archive marker) — fall back to a size comparison.
        try:
            return target.stat().st_size == item.stat().st_size
        except OSError:
            return False

    @classmethod
    def _unique_dest_name(cls, dest_dir: Path, name: str, source: Path) -> Path:
        """Return a path under ``dest_dir`` for ``name`` that does not already
        exist. Prefers appending the source file's artist tag (e.g.
        "Dynamite-BTS.mp3"); falls back to " (2)", " (3)", … if the artist is
        unknown or that name is also taken.
        """
        candidate = dest_dir / name
        if not candidate.exists():
            return candidate
        stem, suffix = os.path.splitext(name)

        artist = cls._read_artist_tag(source)
        if artist:
            safe_artist = sanitize_path(artist)
            candidate = dest_dir / f"{stem}-{safe_artist}{suffix}"
            if not candidate.exists():
                return candidate

        n = 2
        while True:
            candidate = dest_dir / f"{stem} ({n}){suffix}"
            if not candidate.exists():
                return candidate
            n += 1

    @classmethod
    def _move_sync(cls, src: Path, dest: Path, task_dir: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            if dest.exists():
                for item in src.iterdir():
                    target = dest / item.name
                    if target.exists():
                        if (
                            target.is_file()
                            and item.is_file()
                            and cls._is_same_track(target, item)
                        ):
                            # Same filename + matching title/artist tags —
                            # this is a re-download of the exact same track.
                            # Safe to replace, and avoids piling up
                            # duplicates on retries.
                            logger.info(
                                "Overwriting matching track at %s", target
                            )
                        else:
                            # Different content sharing the same filename
                            # (e.g. two different tracks both titled
                            # "Dynamite" landing in the same folder). Never
                            # silently clobber an unrelated file — rename the
                            # incoming one instead, tagging it with its
                            # artist (e.g. "Dynamite-BTS.mp3").
                            new_target = cls._unique_dest_name(
                                dest, item.name, item
                            )
                            logger.warning(
                                "Destination %s already exists with different "
                                "content — saving new file as %s instead of "
                                "overwriting",
                                target,
                                new_target.name,
                            )
                            target = new_target
                    shutil.move(str(item), str(target))
            else:
                shutil.move(str(src), str(dest))

        # Fix ownership so the host user can move/delete files without sudo.
        try:
            for path in dest.rglob("*"):
                os.chown(path, _PUID, _PGID)
                path.chmod(0o755 if path.is_dir() else 0o644)
            os.chown(dest, _PUID, _PGID)
            dest.chmod(0o755)
            # Also fix any parent directories that dest.parent.mkdir() created
            # as root (e.g. Channel/ or Artist/ layers above the final folder).
            # Walk upward and chown every directory still owned by root (uid=0).
            parent = dest.parent
            while True:
                try:
                    if parent.stat().st_uid == 0:
                        os.chown(str(parent), _PUID, _PGID)
                        parent.chmod(0o755)
                        logger.info("Fixed ownership of parent dir: %s", parent)
                    else:
                        break  # reached a dir already owned by the right user
                except Exception:
                    break
                if parent == parent.parent:
                    break
                parent = parent.parent
        except Exception as exc:
            logger.warning("chown failed (non-fatal): %s", exc)

        shutil.rmtree(task_dir, ignore_errors=True)
        return dest

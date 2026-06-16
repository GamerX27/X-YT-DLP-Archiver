import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:1b")
INSTRUCTIONS_FILE = Path(os.getenv("INSTRUCTIONS_FILE", "/app/instructions.md"))
INSTRUCTIONS_JELLYFIN_FILE = Path(
    os.getenv("INSTRUCTIONS_JELLYFIN_FILE", "/app/instructions_jellyfin.md")
)
INSTRUCTIONS_AUDIO_FILE = Path(
    os.getenv("INSTRUCTIONS_AUDIO_FILE", "/app/instructions_audio.md")
)

_UNSAFE_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_name(name: str, max_len: int = 80) -> str:
    return _UNSAFE_PATH_CHARS.sub("_", str(name)).strip(". ")[:max_len]


class LLMAgent:
    def __init__(self) -> None:
        self.model: str = OLLAMA_MODEL
        self._instructions: Optional[str] = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _read_instructions(self) -> str:
        if self._instructions is not None:
            return self._instructions
        try:
            self._instructions = INSTRUCTIONS_FILE.read_text(encoding="utf-8")
        except Exception:
            self._instructions = ""
        return self._instructions

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
        """
        Compute (channel_name, playlist_name, folder_path) directly from metadata.
        Never delegated to the LLM — avoids hallucinated channel names.
        """
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
        Computed entirely from metadata — the LLM never touches folder or channel names
        so it cannot hallucinate them.
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
                # Album download: Artist/Album/
                folder = f"{artist}/{playlist}"
                template = (
                    "%(playlist_index)02d - %(title)s.%(ext)s"
                    if include_playlist_index
                    else "%(title)s.%(ext)s"
                )
            else:
                # Single track: Artist/
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
        """Default format strings when LLM is unavailable."""
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
                "embedthumbnail": True,  # --embed-thumbnail
                "addmetadata": True,  # --add-metadata
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

    def _build_format_prompt(
        self,
        summary: Dict[str, Any],
        resolution_override: Optional[str],
        instructions: str,
    ) -> str:
        """Build the LLM prompt. Always asks only for format_string + extra_opts.
        Folder and output_template are always computed in Python."""
        return (
            "You are a yt-dlp format selector.\n\n"
            f"Video info:\n```json\n{json.dumps(summary, indent=2)}\n```\n\n"
            f"User chose resolution: {resolution_override}\n\n"
            + instructions
            + "\n\nReturn JSON with ONLY these three fields:\n"
            "```json\n"
            "{\n"
            '  "format_string": "yt-dlp format string here",\n'
            '  "extra_opts": {},\n'
            '  "reasoning": "one sentence"\n'
            "}\n"
            "```"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def analyze(
        self,
        metadata: Dict[str, Any],
        resolution_override: Optional[str] = None,
        jellyfin_library_type: Optional[str] = None,
        include_playlist_index: bool = True,
    ) -> Dict[str, Any]:
        summary = self._summarize_metadata(metadata)

        # Folder + template: always computed from metadata in Python.
        # The LLM never supplies these — it cannot hallucinate channel names.
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

        # Default format (used if LLM fails or for audio where opts are fixed)
        fmt, extra = self._format_from_resolution(resolution_override, summary)
        is_audio = resolution_override == "audio"

        # For audio, use the dedicated instructions file so the LLM knows
        # exactly what to return (MP3 postprocessors, embed-thumbnail, etc.).
        if is_audio:
            try:
                instructions = INSTRUCTIONS_AUDIO_FILE.read_text(encoding="utf-8")
            except Exception:
                instructions = ""
        else:
            instructions = self._read_instructions()

        prompt = self._build_format_prompt(summary, resolution_override, instructions)

        logger.debug("LLM prompt:\n%s", prompt)

        reasoning = "Default plan"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{OLLAMA_HOST}/api/chat",
                    json={
                        "model": self.model,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "You are a yt-dlp format selector. "
                                    "Return valid JSON only with keys: "
                                    "format_string, extra_opts, reasoning."
                                ),
                            },
                            {"role": "user", "content": prompt},
                        ],
                        "format": "json",
                        "stream": False,
                        "options": {"temperature": 0.1},
                    },
                    timeout=120,
                )
                resp.raise_for_status()
                raw = resp.json()["message"]["content"]
                logger.info("LLM raw response: %s", raw[:400])

                llm = json.loads(raw)
                if llm.get("format_string"):
                    fmt = llm["format_string"]
                if isinstance(llm.get("extra_opts"), dict):
                    extra = llm["extra_opts"]

                reasoning = llm.get("reasoning", "LLM-selected format")
                logger.info(
                    "LLM selected format: %s  folder: %s  reasoning: %s",
                    fmt[:80],
                    folder,
                    reasoning,
                )

        except Exception as exc:
            reasoning = f"Default plan (LLM error: {exc})"
            logger.warning("LLM format selection failed: %s — using default", exc)

        # For audio, always enforce the correct format and postprocessors
        # regardless of what the LLM returned — it must never produce a video.
        if is_audio:
            fmt = "bestaudio/best"
            extra = {
                **extra,
                # Only the audio extraction postprocessor — thumbnail and
                # metadata are handled by our custom _EmbedPP (_id3_embed)
                # which uses mutagen to write ID3 tags into the MP3.
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "0",
                    }
                ],
            }

        return {
            "channel_name": channel,
            "content_type": "playlist" if is_playlist else "video",
            "playlist_name": playlist or None,
            "folder_path": folder,
            "format_string": fmt,
            "output_template": output_template,
            "extra_opts": extra,
            "reasoning": reasoning,
        }

    async def order_playlist_episodes(self, episodes: list) -> list:
        """
        Given [{filename, playlist_index, upload_date}] sorted by playlist_index,
        return [{filename, new_date}] with dates corrected to be strictly ascending
        so Jellyfin displays episodes in playlist order.
        Falls back to a pure-Python normalization if the LLM call fails.
        """
        from datetime import datetime, timedelta

        def _python_fallback(eps: list) -> list:
            result = []
            prev_date = None
            for ep in eps:
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

        try:
            instructions = INSTRUCTIONS_JELLYFIN_FILE.read_text(encoding="utf-8")
        except Exception:
            instructions = ""

        prompt = (
            instructions.strip()
            + "\n\nEpisode list (sorted by playlist index ascending):\n"
            + json.dumps(episodes, indent=2)
        )

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{OLLAMA_HOST}/api/chat",
                    json={
                        "model": self.model,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "You correct episode dates for Jellyfin ordering. "
                                    'Return JSON: {"episodes": [{"filename": "...", "new_date": "YYYY-MM-DD"}, ...]}'
                                ),
                            },
                            {"role": "user", "content": prompt},
                        ],
                        "format": "json",
                        "stream": False,
                        "options": {"temperature": 0.0},
                    },
                    timeout=120,
                )
                resp.raise_for_status()
                raw = resp.json()["message"]["content"]
                logger.info("LLM ordering response: %s", raw[:400])

                parsed = json.loads(raw)
                # Model may return {"episodes": [...]} or directly [...]
                if isinstance(parsed, list):
                    ordered = parsed
                elif isinstance(parsed, dict):
                    ordered = None
                    for v in parsed.values():
                        if isinstance(v, list):
                            ordered = v
                            break
                    if ordered is None:
                        raise ValueError("No list found in LLM ordering response")
                else:
                    raise ValueError(
                        f"Unexpected LLM ordering response type: {type(parsed)}"
                    )

                if not all(
                    isinstance(i, dict) and "filename" in i and "new_date" in i
                    for i in ordered
                ):
                    raise ValueError("LLM ordering items missing required keys")

                logger.info(
                    "LLM ordering: %d episodes assigned new dates", len(ordered)
                )
                return ordered

        except Exception as exc:
            logger.warning("LLM ordering failed, using Python fallback: %s", exc)
            return _python_fallback(episodes)

    async def ensure_model(self) -> None:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{OLLAMA_HOST}/api/tags", timeout=30)
                resp.raise_for_status()
                names = [m.get("name", "") for m in resp.json().get("models", [])]
                if any(self.model in n for n in names):
                    logger.info("Ollama model %r already present", self.model)
                    return
                logger.info("Pulling Ollama model %r …", self.model)
                await client.post(
                    f"{OLLAMA_HOST}/api/pull",
                    json={"name": self.model},
                    timeout=600,
                )
                logger.info("Ollama model %r pulled successfully", self.model)
        except Exception as exc:
            logger.warning("ensure_model error: %s", exc)

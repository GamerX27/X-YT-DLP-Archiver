import asyncio
import logging
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import disk
import monitors
import state
from downloader import DOWNLOADS_DIR, DownloadCancelled, normalize_playlist_url, sanitize_path

logger = logging.getLogger(__name__)


async def reorder_jellyfin_playlist(dest: Path, planner) -> None:
    """
    Normalize mtimes of downloaded playlist videos in dest so Jellyfin
    orders episodes by playlist index rather than upload date.
    Skipped if dates are already strictly ascending.
    """
    video_exts = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v"}
    files = sorted(
        f for f in dest.rglob("*") if f.is_file() and f.suffix.lower() in video_exts
    )
    if len(files) < 2:
        return

    idx_re = re.compile(r"^(\d+)")
    episodes = []
    for f in files:
        m = idx_re.match(f.name)
        idx = int(m.group(1)) if m else 9999
        try:
            mtime = f.stat().st_mtime
            date_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        except Exception:
            date_str = "2000-01-01"
        episodes.append(
            {
                "filename": f.name,
                "playlist_index": idx,
                "upload_date": date_str,
                "_path": f,
            }
        )

    episodes.sort(key=lambda e: e["playlist_index"])

    dates = [e["upload_date"] for e in episodes]
    if all(dates[i] < dates[i + 1] for i in range(len(dates) - 1)):
        logger.info("Jellyfin ordering: dates already strictly ascending — skipping")
        return

    episodes_input = [
        {
            "filename": e["filename"],
            "playlist_index": e["playlist_index"],
            "upload_date": e["upload_date"],
        }
        for e in episodes
    ]
    ordered = await planner.order_playlist_episodes(episodes_input)
    date_map = {item["filename"]: item["new_date"] for item in ordered}

    for ep in episodes:
        new_date = date_map.get(ep["filename"])
        if not new_date:
            continue
        # Update the embedded date tag AND the mtime together so Jellyfin's
        # displayed date and its sort order stay consistent.
        state.downloader.set_video_date(ep["_path"], new_date)
        logger.info("Jellyfin reorder: %s → %s", ep["filename"], new_date)


async def _download_with_resume(
    download_kwargs: Dict[str, Any],
    archive_file: str,
    task_id: str,
    max_passes: int = 12,
) -> Path:
    """Run the playlist download repeatedly until a pass downloads nothing new.

    YouTube can hand yt-dlp a truncated view of a long playlist (e.g. only the
    first ~100 of 181 entries), and long downloads can also be interrupted by
    transient network/format errors partway through. Because every fetched
    video ID is written to the persistent ``.yt-dlp-archive`` hidden file, we
    can simply run the same download again: yt-dlp skips everything already in
    the archive and continues with whatever remains. We loop until a clean pass
    adds zero new IDs (playlist complete) or we hit ``max_passes``.
    """
    last_dir = DOWNLOADS_DIR / task_id
    for pass_num in range(1, max_passes + 1):
        before = disk.count_archive_entries(archive_file)
        if pass_num > 1:
            state.update_task(
                task_id,
                status_text=f"Resuming playlist… (pass {pass_num}, {before} done)",
            )
        try:
            last_dir = await state.downloader.download(**download_kwargs)
        except DownloadCancelled:
            raise
        except Exception as exc:
            # A full cache drive won't recover by retrying — stop immediately
            # with a clear message instead of looping until max_passes.
            if disk.is_disk_full(exc):
                free_mb = disk.free_disk_mb(DOWNLOADS_DIR)
                raise RuntimeError(
                    "Download cache drive is full (no space left on device, "
                    f"{free_mb} MB free). Free up space and retry."
                ) from exc
            after = disk.count_archive_entries(archive_file)
            # If this pass still managed to fetch new items before failing,
            # resume on the next pass instead of giving up — the archive
            # guarantees we won't re-download what already succeeded.
            if after > before and pass_num < max_passes:
                logger.warning(
                    "Task %s — pass %d fetched %d new item(s) then errored, "
                    "resuming: %s",
                    task_id,
                    pass_num,
                    after - before,
                    exc,
                )
                continue
            raise

        after = disk.count_archive_entries(archive_file)
        new_items = after - before
        logger.info(
            "Task %s — playlist pass %d added %d new item(s) (archive %d → %d)",
            task_id,
            pass_num,
            new_items,
            before,
            after,
        )
        if new_items == 0:
            break
    return last_dir


async def _prepare_browser_download(task_dir: Path, folder_path: str) -> tuple:
    """Package a finished ad-hoc download for the browser to fetch.

    A single video/audio file is left as-is; a playlist (multiple files) is
    zipped as a whole so the user can save it in one click. Returns
    ``(file_path, filename)``.
    """
    safe_folder = "/".join(
        sanitize_path(p) for p in folder_path.split("/") if p.strip()
    )
    src_dir = task_dir / safe_folder if safe_folder else task_dir
    if not src_dir.is_dir():
        raise RuntimeError("Downloaded file could not be located")

    files = [f for f in src_dir.rglob("*") if f.is_file() and not f.name.startswith(".")]
    if not files:
        raise RuntimeError("Downloaded file could not be located")

    if len(files) == 1:
        return files[0], files[0].name

    zip_stem = sanitize_path(safe_folder.rstrip("/").split("/")[-1] or "playlist")
    base_name = str(task_dir / zip_stem)
    loop = asyncio.get_event_loop()
    zip_path_str = await loop.run_in_executor(
        None,
        lambda: shutil.make_archive(
            base_name, "zip", root_dir=str(src_dir.parent), base_dir=src_dir.name
        ),
    )
    zip_path = Path(zip_path_str)
    shutil.rmtree(src_dir, ignore_errors=True)
    return zip_path, zip_path.name


async def process_task(task_id: str) -> None:
    task = state.tasks.get(task_id)
    if task is None:
        return

    abort_event = threading.Event()
    state.task_abort_events[task_id] = abort_event

    def _cancelled() -> bool:
        if abort_event.is_set():
            shutil.rmtree(DOWNLOADS_DIR / task_id, ignore_errors=True)
            state.update_task(
                task_id, status="cancelled", status_text="Cancelled", progress=0
            )
            return True
        return False

    try:
        if _cancelled():
            return
        resolved_url = normalize_playlist_url(task["url"])
        if resolved_url != task["url"]:
            logger.info("Task %s — using full playlist URL: %s", task_id, resolved_url)
        else:
            logger.info("Task %s — starting: %s", task_id, resolved_url)
        state.update_task(task_id, status="probing", status_text="Probing URL…")
        metadata = await state.downloader.probe_url(task["url"])
        title = metadata.get("title") or task["url"]
        channel = metadata.get("channel") or metadata.get("uploader") or "Unknown"
        state.update_task(task_id, title=title, channel=channel)

        if _cancelled():
            return
        state.update_task(task_id, status="analyzing", status_text="Planning download…")
        plan = await state.format_planner.analyze(
            metadata,
            task.get("resolution_override"),
            jellyfin_library_type=task.get("jellyfin_library_type"),
            include_playlist_index=task.get("include_playlist_index", True),
        )
        state.update_task(
            task_id,
            folder=plan.get("folder_path", "Downloads"),
            format_string=plan.get("format_string", "bestvideo+bestaudio/best"),
            status_text=f"Plan ready: {plan.get('reasoning', '')}",
        )

        # If the user manually selected a destination subfolder in the browser,
        # override the computed folder and use a simple filename template.
        if task.get("folder_override"):
            plan["folder_path"] = task["folder_override"]
            # For a single track going into an existing folder, keep the title only.
            if plan.get("content_type") != "playlist":
                plan["output_template"] = "%(title)s.%(ext)s"
            state.update_task(task_id, folder=plan["folder_path"])

        # For playlists: use a persistent download archive so yt-dlp skips
        # videos already downloaded on previous runs of the same playlist.
        #
        # The persistent archive lives in the DESTINATION folder, but we only
        # commit IDs to it AFTER their files have actually been moved there.
        # Videos download into a temporary staging dir first; if the run fails
        # between download and move (e.g. the cache drive fills up), writing the
        # archive directly in the destination would record IDs whose files never
        # arrived — permanently skipping those episodes on the next run. So
        # during the download we use a *staging* archive seeded from the
        # persistent one, and commit it only on success (see after the move).
        # A download only gets a persistent server-side destination (and thus a
        # persistent cross-run archive) when it's writing into a Jellyfin
        # library. An ad-hoc download with none selected streams straight to
        # the user's browser instead — there is nothing on disk to seed an
        # archive from, and nothing worth keeping around after the user saves
        # the file. Monitors always have a Jellyfin destination (the Monitor
        # tab is unusable without Jellyfin configured — see below).
        browser_download = not task.get("jellyfin_library_path") and not task.get(
            "monitor_id"
        )

        if task.get("monitor_id") and not task.get("jellyfin_library_path"):
            msg = "This monitor has no Jellyfin destination configured."
            state.update_task(task_id, status="failed", status_text=msg, error=msg)
            return

        dest_archive: Optional[Path] = None
        staging_archive: Optional[Path] = None
        if plan.get("content_type") == "playlist":
            staging_archive = DOWNLOADS_DIR / task_id / ".yt-dlp-archive"
            try:
                staging_archive.parent.mkdir(parents=True, exist_ok=True)
                if not browser_download:
                    archive_base = Path(task["jellyfin_library_path"])
                    dest_archive = (
                        archive_base / plan.get("folder_path", "") / ".yt-dlp-archive"
                    )
                    # Seed the staging archive with everything already in the
                    # destination so we don't re-download completed episodes.
                    if dest_archive.exists():
                        shutil.copyfile(dest_archive, staging_archive)
                    logger.info(
                        "Playlist archive: staging=%s commits to %s after move",
                        staging_archive,
                        dest_archive,
                    )
                plan["extra_opts"]["download_archive"] = str(staging_archive)
            except Exception as exc:
                logger.warning("Could not set up download archive (non-fatal): %s", exc)

        if _cancelled():
            return

        # Preflight: refuse to start if the staging drive is already low on
        # space, so we fail fast with a clear message instead of part-way
        # through with an ENOSPC error (and a half-written staging dir).
        free_mb = disk.free_disk_mb(DOWNLOADS_DIR)
        if 0 <= free_mb < state.MIN_FREE_DISK_MB:
            msg = (
                f"Not enough free disk space on the download cache drive: "
                f"{free_mb} MB free, need at least {state.MIN_FREE_DISK_MB} MB."
            )
            logger.error("Task %s — %s", task_id, msg)
            state.update_task(
                task_id, status="failed", status_text=msg, error=msg, progress=0
            )
            return

        is_audio = task.get("resolution_override") == "audio"
        state.update_task(task_id, status="downloading", status_text="Downloading…")

        async def progress_cb(
            percent: float,
            speed: str,
            eta: str,
            filename: str,
            playlist_index: Optional[int] = None,
            playlist_count: Optional[int] = None,
        ) -> None:
            if playlist_index and playlist_count:
                status_text = f"{playlist_index}/{playlist_count} · {percent:.1f}%"
            else:
                status_text = f"Downloading… {percent:.1f}%"
            state.update_task(
                task_id,
                progress=percent,
                speed=speed,
                eta=eta,
                filename=filename,
                playlist_index=playlist_index,
                playlist_count=playlist_count,
                status_text=status_text,
            )

        download_kwargs: Dict[str, Any] = dict(
            url=task["url"],
            format_string=plan.get("format_string", "bestvideo+bestaudio/best"),
            output_template=plan.get("output_template", "%(title)s [%(id)s].%(ext)s"),
            extra_opts=plan.get("extra_opts", {}),
            folder_path=plan.get("folder_path", "Downloads"),
            task_id=task_id,
            progress_cb=progress_cb,
            abort_event=abort_event,
            is_audio=is_audio,
            resolution_override=task.get("resolution_override"),
        )

        # For playlists with a download archive, keep re-running the download
        # until a pass adds nothing new. yt-dlp skips already-archived IDs, so
        # each pass resumes where the previous one left off — completing
        # playlists that YouTube only exposed partially and recovering from
        # mid-playlist interruptions.
        archive_file = plan.get("extra_opts", {}).get("download_archive")
        try:
            if plan.get("content_type") == "playlist" and archive_file:
                task_dir = await _download_with_resume(
                    download_kwargs, archive_file, task_id
                )
            else:
                task_dir = await state.downloader.download(**download_kwargs)
        except DownloadCancelled:
            shutil.rmtree(DOWNLOADS_DIR / task_id, ignore_errors=True)
            state.update_task(
                task_id, status="cancelled", status_text="Cancelled", progress=0
            )
            return

        if _cancelled():
            return

        if browser_download:
            # No Jellyfin library configured for this download — stage the
            # finished file(s) in place and let the user pull it down through
            # the browser instead of writing it to the server.
            state.update_task(
                task_id,
                status="finalizing",
                status_text="Preparing file…",
                progress=100,
            )
            file_path, download_filename = await _prepare_browser_download(
                task_dir, plan.get("folder_path", "Downloads")
            )
            logger.info("Task %s ready for browser download: %s", task_id, file_path)
            state.update_task(
                task_id,
                status="completed",
                status_text="Ready for download",
                final_path=None,
                browser_ready=True,
                download_filename=download_filename,
                progress=100,
                _completed_at=datetime.now().isoformat(),
            )
            return

        # Guaranteed set here: browser_download (no Jellyfin, no monitor) and
        # monitor-without-Jellyfin both returned above.
        dest_dir = task["jellyfin_library_path"]

        state.update_task(
            task_id,
            status="moving",
            status_text="Moving to Jellyfin folder…",
            progress=100,
        )

        # Snapshot the staging archive before the move (which deletes the
        # staging dir). We commit it to the destination only after the move
        # succeeds, so the archive can never get ahead of the files on disk.
        archive_snapshot: Optional[str] = None
        if staging_archive is not None and staging_archive.exists():
            try:
                archive_snapshot = staging_archive.read_text(encoding="utf-8")
            except OSError:
                archive_snapshot = None

        final_dest = await state.downloader.move_to_media(
            task_dir=task_dir,
            folder_path=plan.get("folder_path", "Downloads"),
            media_dir=dest_dir,
        )

        # Commit the archive now that the files are actually in the destination.
        if archive_snapshot is not None and dest_archive is not None:
            try:
                dest_archive.parent.mkdir(parents=True, exist_ok=True)
                dest_archive.write_text(archive_snapshot, encoding="utf-8")
            except OSError as exc:
                logger.warning("Could not commit download archive (non-fatal): %s", exc)

        # Reorder before triggering the library scan below — Jellyfin ingests
        # each file's date at scan time, so fixing mtimes after the scan has
        # already run leaves the wrong order cached in Jellyfin's database
        # until some later, unrelated rescan happens to pick it up.
        if (
            plan.get("content_type") == "playlist"
            and task.get("jellyfin_library_path")
            and not is_audio
        ):
            state.update_task(
                task_id,
                status="ordering",
                status_text="Ordering episodes for Jellyfin…",
            )
            try:
                await reorder_jellyfin_playlist(final_dest, state.format_planner)
            except Exception as exc:
                logger.warning(
                    "Jellyfin episode reordering failed (non-fatal): %s", exc
                )

        jf_lib_id = task.get("jellyfin_library_id")
        if jf_lib_id:
            try:
                await state.jellyfin_client.refresh_library(jf_lib_id)
                logger.info("Jellyfin library %s refresh triggered", jf_lib_id)
            except Exception as jf_exc:
                logger.warning("Jellyfin refresh failed (non-fatal): %s", jf_exc)

        state.update_task(
            task_id,
            status="completed",
            status_text="Done",
            final_path=str(final_dest),
            progress=100,
        )

        # If triggered by a monitor, record the exact folder_path used so
        # future archive checks know exactly where to look.
        if task.get("monitor_id"):
            _mon = state.monitor_store.get(task["monitor_id"]) or {}
            fresh_count = monitors.archive_count(
                {**_mon, "monitor_folder_path": plan.get("folder_path")}
            )
            state.monitor_store.update(
                task["monitor_id"],
                monitor_folder_path=plan.get("folder_path"),
                archive_count=fresh_count,
                last_new_videos=max(0, fresh_count - _mon.get("archive_count", 0)),
            )
            await state.broadcast(
                {"type": "monitors_update", "monitors": state.monitor_store.all()}
            )

    except Exception as exc:
        if disk.is_disk_full(exc):
            free_mb = disk.free_disk_mb(DOWNLOADS_DIR)
            msg = (
                f"Download cache drive is full (no space left on device, "
                f"{free_mb} MB free). Free up space and retry."
            )
            logger.error("Task %s failed — %s", task_id, msg)
            state.update_task(task_id, status="failed", status_text=msg, error=msg)
        else:
            logger.exception("Task %s failed", task_id)
            state.update_task(task_id, status="failed", status_text="Failed", error=str(exc))
    finally:
        state.task_abort_events.pop(task_id, None)


async def worker() -> None:
    while True:
        task_id = await state.task_queue.get()
        async with state.semaphore:
            try:
                await process_task(task_id)
            except Exception:
                logger.exception("Unhandled error in worker for task %s", task_id)
            finally:
                state.task_queue.task_done()

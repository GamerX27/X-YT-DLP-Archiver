import asyncio
import logging
import shutil
from datetime import datetime, timedelta

import monitors
import state
from downloader import DOWNLOADS_DIR

logger = logging.getLogger(__name__)


async def monitor_scheduler() -> None:
    """Background loop — checks every 60 s and triggers due monitors."""
    while True:
        await asyncio.sleep(60)
        try:
            for monitor in state.monitor_store.due():
                await monitors.run_monitor(monitor)
                state.monitor_store.mark_ran(monitor["id"])
        except Exception as exc:
            logger.exception("Monitor scheduler error: %s", exc)


async def cleanup_scheduler() -> None:
    """Background loop — expires "browser download" files nobody came back
    for, so the download cache drive doesn't fill up with abandoned files."""
    while True:
        await asyncio.sleep(3600)
        try:
            now = datetime.now()
            for task_id, task in list(state.tasks.items()):
                if not task.get("browser_ready"):
                    continue
                completed_at = task.get("_completed_at")
                if not completed_at:
                    continue
                try:
                    completed_dt = datetime.fromisoformat(completed_at)
                except ValueError:
                    continue
                if now - completed_dt > timedelta(hours=state.BROWSER_DOWNLOAD_TTL_HOURS):
                    shutil.rmtree(DOWNLOADS_DIR / task_id, ignore_errors=True)
                    state.tasks.pop(task_id, None)
                    await state.broadcast({"type": "task_removed", "task_id": task_id})
                    logger.info(
                        "Expired unclaimed browser download %s after %dh",
                        task_id,
                        state.BROWSER_DOWNLOAD_TTL_HOURS,
                    )
        except Exception:
            logger.exception("Cleanup scheduler error")

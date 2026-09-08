import errno
import shutil
from pathlib import Path
from typing import Optional


def free_disk_mb(path: Path) -> int:
    """Free space (MB) on the filesystem holding ``path``; -1 if unknown."""
    try:
        return shutil.disk_usage(path).free // (1024 * 1024)
    except OSError:
        return -1


def is_disk_full(exc: BaseException) -> bool:
    """True if an exception (or its chain) is a 'no space left on device' error.

    yt-dlp wraps the underlying ``OSError`` in a ``DownloadError`` whose message
    still carries the text, so check both the errno and the string form.
    """
    seen = exc
    while seen is not None:
        if isinstance(seen, OSError) and seen.errno == errno.ENOSPC:
            return True
        seen = seen.__cause__ or seen.__context__
    return "no space left on device" in str(exc).lower()


def count_archive_entries(archive_path: Optional[str]) -> int:
    """Number of video IDs currently recorded in a yt-dlp download archive.

    Used to detect whether a download pass actually fetched anything new.
    """
    if not archive_path:
        return 0
    try:
        with open(archive_path, "r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0

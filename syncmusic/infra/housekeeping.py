# SPDX-License-Identifier: GPL-3.0-or-later
"""Housekeeping (Settings → Cache & space): rotated log files past their age, and the low-disk check
of the disk holding the app's data and cache."""

from __future__ import annotations

import shutil
import time
from pathlib import Path


def prune_logs(log_dir: Path, days: int, now: float | None = None) -> int:
    """Delete rotated log files (`syncmusic.log.1` …) last written more than `days` ago; the current log stays.
    Returns how many were deleted."""
    if not log_dir.is_dir():
        return 0
    cut = (time.time() if now is None else now) - days * 86400
    removed = 0
    for f in log_dir.glob("syncmusic.log.*"):
        try:
            if f.is_file() and f.stat().st_mtime < cut:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def disk_free(path: Path) -> tuple[int, int]:
    """(total, free) bytes of the file system holding `path`."""
    usage = shutil.disk_usage(path)
    return usage.total, usage.free


class LowDisk:
    """Says once when the free space drops below the limit, and again only after it was above it."""

    def __init__(self) -> None:
        self.low = False

    def crossed(self, free: int, limit: int | None) -> bool:
        if limit is None:
            self.low = False
            return False
        now_low = free < limit
        crossed = now_low and not self.low
        self.low = now_low
        return crossed

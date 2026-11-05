# SPDX-License-Identifier: GPL-3.0-or-later
"""Small helpers shared by processors (plain functions, no base class)."""

from __future__ import annotations

import shutil
from pathlib import Path

from syncmusic.core.models import WorkItem


def owns(item: WorkItem) -> bool:
    """True when the item's file lives in the work dir (produced by an earlier step), so it may be modified.

    Anything else - a library original, a cache entry - must never be changed in place.
    """
    return item.path.parent.resolve() == item.work_dir.resolve()


def work_copy(item: WorkItem, step: str) -> Path:
    """A file the processor may modify: the item's own work file, or a fresh copy in the work dir.

    Blocking (file copy): call via `asyncio.to_thread`.
    """
    if owns(item):
        return item.path
    item.work_dir.mkdir(parents=True, exist_ok=True)
    dst = item.work_dir / f"{item.path.stem}.{step}{item.path.suffix}"
    shutil.copyfile(item.path, dst)
    return dst

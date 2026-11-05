# SPDX-License-Identifier: GPL-3.0-or-later
"""Watch a local folder for changes: inotify through `watchfiles`, debounced - `on_change` runs once
the folder has been quiet for `quiet_s` after a change (a copy of a whole album is one call, not hundreds)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from watchfiles import awatch

log = logging.getLogger(__name__)

QUIET_S = 30.0  # wait this long after the last change
MAX_WAIT_S = 300.0  # but no longer than this while changes keep coming


async def watch_folder(
    root: Path,
    on_change: Callable[[], Awaitable[None]],
    quiet_s: float | None = None,
    max_wait_s: float | None = None,
) -> None:
    """Runs until cancelled. A folder that cannot be watched (gone, the inotify watch limit `max_user_watches`
    reached) is logged and given up on: the periodic snapshot refresh still sees its changes."""
    quiet_s = QUIET_S if quiet_s is None else quiet_s
    max_wait_s = MAX_WAIT_S if max_wait_s is None else max_wait_s
    if not await asyncio.to_thread(root.is_dir):  # awatch would wait for it forever
        log.warning("cannot watch %s for changes (periodic refresh only): not a folder", root)
        return
    try:
        async for _changes in awatch(
            root,
            step=int(quiet_s * 1000),
            debounce=int(max_wait_s * 1000),
            recursive=True,
            ignore_permission_denied=True,
        ):
            await on_change()
    except (OSError, RuntimeError) as e:
        log.warning("cannot watch %s for changes (periodic refresh only): %s", root, e)

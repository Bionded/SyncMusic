# SPDX-License-Identifier: GPL-3.0-or-later
"""Desktop mode: the web UI in a window of its own, with the daemon in the same process.

The window is a Chromium-family browser in app mode (`--app=URL`: no tabs, no address bar, its own window and
taskbar entry) with a profile of its own under the data dir, so it is a separate process that exits when the
window is closed. Prebuilt on every platform (arm64 included), nothing to compile. The browser is
`SYNCMUSIC_BROWSER` or the first of `BROWSERS` on PATH. Without one, the default browser opens the UI in a tab;
that cannot report its closing, so the application then runs until it is stopped (Ctrl+C / signal).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import webbrowser
from collections.abc import Mapping
from pathlib import Path

from syncmusic.infra.config import env_value

log = logging.getLogger(__name__)

BROWSERS = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "microsoft-edge",
    "brave-browser",
)


def find_browser(env: Mapping[str, str] | None = None) -> str | None:
    if browser := env_value("BROWSER", env):
        return browser
    return next((p for name in BROWSERS if (p := shutil.which(name))), None)


def app_window_args(browser: str, url: str, profile: Path) -> list[str]:
    args = [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",  # own profile: never handed over to a running browser
        "--no-first-run",
        "--no-default-browser-check",
        "--class=SyncMusic",
        "--window-size=1280,860",
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")  # Chromium refuses to start as root otherwise
    return args


async def run_window(url: str, stop: asyncio.Event, profile: Path) -> int | None:
    """Show the UI until the window is closed (then sets `stop`) or `stop` is set by the application (then closes
    the window). Returns the browser's exit code when it ended by itself (non-zero: it failed)."""
    browser = find_browser()
    if browser is None:
        log.warning(
            "no Chromium-family browser found (set SYNCMUSIC_BROWSER): opening %s in the default browser; "
            "stop SyncMusic with Ctrl+C",
            url,
        )
        await asyncio.to_thread(webbrowser.open, url)
        await stop.wait()
        return None
    await asyncio.to_thread(profile.mkdir, parents=True, exist_ok=True)
    log.info("desktop window: %s", browser)
    proc = await asyncio.create_subprocess_exec(
        *app_window_args(browser, url, profile),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    waiter = asyncio.ensure_future(proc.wait())
    stopper = asyncio.ensure_future(stop.wait())
    code: int | None = None
    try:
        await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if waiter.done():
            code = waiter.result()
    finally:
        stopper.cancel()
        if not waiter.done():
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(asyncio.shield(waiter), 10)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await waiter
        stop.set()
        log.info("desktop window closed")
    return code

"""Player contents is still being read when the page is reloaded - the reloaded page waits for the
same read (only one runs on the server) and shows its result. The slow read is a fake on the app's loop.

Reads are counted while the first one is held: once it is released, a later re-render (e.g. a job of the device that
finishes late on a loaded Pi) may legitimately read again."""

from __future__ import annotations

import asyncio
import traceback

import pytest

from syncmusic.devices.flashdrive import FlashDrive
from tests.webui.conftest import open_page, poll, run_on
from tests.webui.test_player_browser import _stick

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def test_contents_read_survives_a_reload(web_server, chrome, tmp_path, monkeypatch):
    url, app = web_server
    _stick(url, app, tmp_path)
    real = FlashDrive.read_contents
    reads: list[int] = []  # Player contents reads of the device
    requests: list[int] = []  # Player contents requests that reached the server
    state: dict[str, asyncio.Event] = {}

    async def setup() -> None:
        state["release"] = asyncio.Event()

    async def slow(self):
        if any(
            f.name == "_read_contents" for f in traceback.extract_stack()
        ):  # not the thumbnails' lookups
            reads.append(1)
        await state["release"].wait()
        return await real(self)

    async def release() -> None:
        state["release"].set()

    async def run_ready() -> None:
        """Let the app's loop run what is ready: a second read the reload started would be under way after it."""
        for _ in range(20):
            await asyncio.sleep(0)

    real_contents = app.sync.contents

    async def counted(device_id, refresh=False):
        requests.append(1)
        return await real_contents(device_id, refresh)

    run_on(app, setup())
    monkeypatch.setattr(FlashDrive, "read_contents", slow)
    monkeypatch.setattr(app.sync, "contents", counted)
    page = open_page(chrome, url + "/#/devices/stick/contents")
    poll(page, lambda: reads, what="the first request to reach the device")
    first = len(requests)
    page.reload()
    poll(page, lambda: len(requests) > first, what="the reloaded page to ask for the contents")
    run_on(app, run_ready())
    held = len(reads)  # before the release: only the first read may have started
    run_on(app, release())
    page.wait_for_selector('input[name="pcItem"]', timeout=10_000)
    assert page.locator('input[name="pcItem"]').count() == 4
    assert held == 1, "the reload started a second read"

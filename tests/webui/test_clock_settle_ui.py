"""Player contents opened while the device is 'Setting the clock…' shows the items once the clock settle
is over - without a page reload. The settle is a short fake clock task on the app's loop. Temp folders only."""

from __future__ import annotations

import asyncio

import pytest

from tests.webui.conftest import open_page, run_on
from tests.webui.test_player_browser import _stick

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def test_contents_appear_when_the_clock_settle_ends(web_server, chrome, tmp_path):
    url, app = web_server
    _stick(url, app, tmp_path)
    release = asyncio.Event()

    async def settle() -> bool:
        await release.wait()
        return True

    async def start() -> None:
        task = asyncio.create_task(settle())
        task.add_done_callback(lambda t: app.sync._clock_done("stick", t))
        app.sync._clock_tasks["stick"] = task

    async def finish() -> None:
        release.set()

    run_on(app, start())
    page = open_page(chrome, url + "/#/devices/stick/contents")
    page.wait_for_selector("text=Setting the clock")
    assert page.locator('input[name="pcItem"]').count() == 0
    run_on(app, finish())
    page.wait_for_selector('input[name="pcItem"]', timeout=10_000)
    assert page.locator('input[name="pcItem"]').count() == 4
    assert not page.locator("text=Setting the clock").count()

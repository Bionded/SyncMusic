"""Library browsing from the in-memory cache: the second open is instant, an expired cache is shown at once
and the page updates when the background reload lands, Refresh reloads now with progress, "Updated N min ago".
A folder library whose snapshot is slowed down stands in for a slow server. Temp folders only."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import httpx
import pytest

from syncmusic.core.models import utcnow
from syncmusic.libraries.folder import FolderLibrary
from tests.media import make_music
from tests.webui.conftest import open_page, run_on

pytestmark = [pytest.mark.ui, pytest.mark.slow]

SLOW_S = 5.0
INSTANT_S = 3.0  # well below SLOW_S; loose enough for the loaded Pi


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.fixture
def slow_library(web_server, tmp_path, monkeypatch):
    url, app = web_server
    real = FolderLibrary.snapshot

    async def slow(self):
        await asyncio.sleep(SLOW_S)
        return await real(self)

    monkeypatch.setattr(FolderLibrary, "snapshot", slow)
    music = tmp_path / "music"
    make_music(music / "a.mp3", title="Перша", artist="Скрябін", album="A")
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
    )
    return url, app, music


async def _refresh(app):
    await app.sync.snapshots.refresh_soon(await app.sync.library("music"))


def _rows(page):
    return page.locator('#content input[name="sel"]').count()


def test_cached_browsing_updates_and_refresh(slow_library, chrome):
    url, app, music = slow_library
    page = open_page(chrome, url + "/#/libraries")
    page.wait_for_selector('[data-updated="music"]')
    assert page.locator('[data-updated="music"]').inner_text() == "Not loaded yet"
    # first open after the start waits for the library
    start = time.monotonic()
    page.goto(url + "/#/libraries/music/browse")
    page.wait_for_selector('input[name="sel"]', timeout=20_000)
    assert time.monotonic() - start >= SLOW_S - 0.5
    assert page.locator('[data-updated="music"]').inner_text() == "Updated just now"
    # second open: instant
    page.goto(url + "/#/libraries")
    page.wait_for_selector('[data-updated="music"]:has-text("Updated just now")')
    start = time.monotonic()
    page.goto(url + "/#/libraries/music/browse")
    page.wait_for_selector('input[name="sel"]')
    assert time.monotonic() - start < INSTANT_S
    # 31 minutes later with a new song: the old list at once, then the new one without a click
    make_music(music / "b.mp3", title="Друга", artist="Скрябін", album="A")
    app.sync.snapshots._checked["music"] = utcnow() - timedelta(minutes=31)
    page.goto(url + "/#/libraries")
    page.wait_for_selector('[data-updated="music"]:has-text("Updated 31 min ago")')
    start = time.monotonic()
    page.goto(url + "/#/libraries/music/browse")
    page.wait_for_selector('input[name="sel"]')
    assert time.monotonic() - start < INSTANT_S and _rows(page) == 1
    page.wait_for_function(
        "() => document.querySelectorAll('#content input[name=sel]').length === 2",
        timeout=20_000,
    )
    assert page.locator('[data-updated="music"]').inner_text() == "Updated just now"
    # with a selection the page is not redrawn: it offers the new data
    make_music(music / "c.mp3", title="Третя", artist="Скрябін", album="A")
    page.locator('input[name="sel"]').first.check()
    app.sync.snapshots._checked["music"] = utcnow() - timedelta(minutes=31)
    run_on(app, _refresh(app), 20)
    page.wait_for_selector("[data-new-data]", timeout=10_000)
    assert page.locator('input[name="sel"]:checked').count() == 1
    page.locator("[data-new-data] button").click()
    page.wait_for_function(
        "() => document.querySelectorAll('#content input[name=sel]').length === 3",
        timeout=10_000,
    )
    # Refresh: reloads now, says so while it runs
    make_music(music / "d.mp3", title="Четверта", artist="Скрябін", album="A")
    button = page.locator('.pagehead [data-action="refresh"]')
    button.click()
    page.wait_for_selector('.pagehead [data-action="refresh"]:has-text("Refreshing…")')
    assert button.is_disabled()
    page.wait_for_function(
        "() => document.querySelectorAll('#content input[name=sel]').length === 4",
        timeout=20_000,
    )
    page.wait_for_selector('.pagehead [data-action="refresh"]:has-text("Refresh")')
    assert page.errors == []

"""Local folder libraries are watched for changes."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.facade import AppFacade
from syncmusic.infra import watch
from tests.media import make_music


async def test_watch_folder_debounces(tmp_path):
    calls: list[int] = []

    async def changed() -> None:
        calls.append(1)

    task = asyncio.create_task(watch.watch_folder(tmp_path, changed, quiet_s=0.3))
    await asyncio.sleep(0.3)  # the watch is set up
    for i in range(5):
        (tmp_path / f"{i}.txt").write_text("x")
    for _ in range(50):
        if calls:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.5)
    task.cancel()
    assert calls == [1]  # five files, one call


async def test_missing_folder_is_given_up_quietly(tmp_path, caplog):
    async def changed() -> None:
        raise AssertionError("never")

    await asyncio.wait_for(watch.watch_folder(tmp_path / "gone", changed, quiet_s=0.1), 5)
    assert "cannot watch" in caplog.text


@pytest.fixture
async def app_with_folder(config, tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "QUIET_S", 0.3)
    root = tmp_path / "music"
    make_music(root / "a.mp3", title="Перша", artist="Скрябін")
    app = Application(config)
    await app.start()
    api = create_api(AppFacade(app), manage_lifecycle=False)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
        body = {"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(root)}}
        assert (await c.post("/api/v1/libraries", json=body)).status_code == 201
        yield app, c, root
    await app.stop()


async def test_a_new_file_reloads_the_snapshot(app_with_folder):
    app, c, root = app_with_folder
    assert set(app.sync._watchers) == {"music"}
    await app.sync.snapshots.get(await app.sync.library("music"))
    events = app.ctx.bus.subscribe()
    await asyncio.sleep(0.3)
    make_music(root / "b.mp3", title="Друга", artist="Скрябін")
    ev = None
    for _ in range(100):
        try:
            ev = await asyncio.wait_for(events.get(), 0.2)
        except TimeoutError:
            continue
        if ev.type == "library.updated":
            break
    assert ev is not None and ev.type == "library.updated"
    snap = await app.sync.snapshots.get(await app.sync.library("music"))
    assert sorted(t.title for t in snap.tracks.values()) == ["Друга", "Перша"]


async def test_watch_off_or_remote_is_not_watched(app_with_folder):
    app, c, root = app_with_folder
    r = await c.patch(
        "/api/v1/libraries/music", json={"settings": {"root": str(root), "watch": False}}
    )
    assert r.status_code == 200, r.text
    assert app.sync._watchers == {}
    r = await c.patch(
        "/api/v1/libraries/music", json={"settings": {"root": str(root), "watch": True}}
    )
    assert set(app.sync._watchers) == {"music"}
    assert (await c.delete("/api/v1/libraries/music")).status_code in (200, 204)
    assert app.sync._watchers == {}

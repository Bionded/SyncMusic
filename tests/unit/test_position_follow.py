"""The device's bookmark follows progress made on another client (docs/08 Positions): a library-only
position change moves the iPod's bookmark and the manifest's last-known position; a bookmark moved on the device
wins (pushed, never overwritten)."""

# ruff: noqa: F811 - fixtures imported from test_position_roundtrip

from __future__ import annotations

import logging
from datetime import datetime

import httpx
import pytest

from syncmusic.core import manifest as manifest_io
from syncmusic.libraries.audiobookshelf import AudiobookshelfLibrary
from tests.unit.test_position_roundtrip import (  # noqa: F401 - fixtures
    KEY,
    _abs_position,
    _book_position,
    _episode,
    _sync,
    chapter_files,
    env,
    server,
)


async def test_library_position_moves_the_device_bookmark(env, server):
    app, c, mount = env
    server.set_progress("p1", 70.0, episode="e1")  # 20 s more on the phone
    patches = len(server.patches)
    report = await _sync(app, c)
    assert report["reverse"]["positions_pushed"] == 0 and len(server.patches) == patches
    _, t = _episode(mount)
    assert t.bookmark_time == 70_000
    assert manifest_io.read(mount).items[KEY].last_position_s == pytest.approx(70.0)
    report = await _sync(app, c)  # the moved bookmark is not a device change: nothing pushed
    assert report["reverse"]["positions_pushed"] == 0
    assert _abs_position(server) == pytest.approx(70.0)


async def test_device_bookmark_wins_over_a_library_change(env, server):
    app, c, mount = env
    server.set_progress("p1", 70.0, episode="e1")  # on the phone ...
    db, t = _episode(mount)
    t.bookmark_time = 90_000  # ... and later on the iPod
    db.save()
    report = await _sync(app, c)
    assert report["reverse"]["positions_pushed"] == 1
    assert _abs_position(server) == pytest.approx(90.0)
    _, t = _episode(mount)
    assert t.bookmark_time == 90_000
    assert manifest_io.read(mount).items[KEY].last_position_s == pytest.approx(90.0)


async def test_newer_library_position_wins_when_both_moved(env, server, caplog):
    """listened on the iPod first, later further on the phone: the phone's position goes to the iPod and the
    iPod's older one is not sent; the next sync changes nothing on either side (no ping-pong)."""
    app, c, mount = env
    db, t = _episode(mount)
    t.bookmark_time = 60_000
    t.time_played = datetime(
        2023, 1, 1, 12, 0
    )  # before the phone's progress (FakeABS clock: Nov 2023)
    db.save()
    server.set_progress("p1", 75.0, episode="e1")
    patches = len(server.patches)
    with caplog.at_level(logging.INFO, logger="syncmusic.core.reverse"):
        report = await _sync(app, c)
    assert (
        report["reverse"]["positions_pushed"] == 0
        and report["reverse"]["positions_superseded"] == 1
    )
    assert len(server.patches) == patches and _abs_position(server) == pytest.approx(75.0)
    assert "position conflict for abs/p1:e1" in caplog.text and "library wins" in caplog.text
    _, t = _episode(mount)
    assert t.bookmark_time == 75_000
    assert manifest_io.read(mount).items[KEY].last_position_s == pytest.approx(75.0)
    report = await _sync(app, c)
    assert report["reverse"]["positions_pushed"] == report["reverse"]["positions_superseded"] == 0
    assert len(server.patches) == patches
    _, t = _episode(mount)
    assert t.bookmark_time == 75_000


async def test_newer_device_position_wins_when_both_moved(env, server, caplog):
    app, c, mount = env
    server.set_progress("p1", 75.0, episode="e1")
    db, t = _episode(mount)
    t.bookmark_time = 85_000
    t.time_played = datetime(2025, 1, 1, 12, 0)  # after the phone's progress
    db.save()
    with caplog.at_level(logging.INFO, logger="syncmusic.core.reverse"):
        report = await _sync(app, c)
    assert (
        report["reverse"]["positions_pushed"] == 1
        and report["reverse"]["positions_superseded"] == 0
    )
    assert "device wins" in caplog.text
    assert _abs_position(server) == pytest.approx(85.0)
    _, t = _episode(mount)
    assert t.bookmark_time == 85_000
    patches = len(server.patches)
    report = await _sync(app, c)
    assert report["reverse"]["positions_pushed"] == 0 and len(server.patches) == patches


async def test_pending_device_bookmark_is_not_overwritten(env, server, monkeypatch):
    app, c, mount = env
    db, t = _episode(mount)
    t.bookmark_time = 90_000  # listened on the iPod while ABS does not take progress
    db.save()

    async def down(self, ref, position_s, finished):
        raise httpx.ConnectError("down")

    with monkeypatch.context() as m:
        m.setattr(AudiobookshelfLibrary, "set_position", down)
        report = await _sync(app, c)
    assert report["reverse"]["pending"] == 1
    _, t = _episode(mount)
    assert t.bookmark_time == 90_000
    server.set_progress(
        "p1", 70.0, episode="e1"
    )  # older progress elsewhere; the device's is still pending
    report = await _sync(app, c)
    _, t = _episode(mount)
    assert t.bookmark_time == 90_000
    assert report["reverse"]["positions_pushed"] == 1 and _abs_position(server) == pytest.approx(
        90.0
    )


async def test_audiobook_chapter_bookmark_follows_the_book(env, server):
    app, c, mount = env
    server.set_progress("b1", 85.0)  # 25 s into chapter 2 on another client
    report = await _sync(app, c)
    assert report["reverse"]["positions_pushed"] == 0
    assert manifest_io.read(mount).items["abs/b1:b1-2"].last_position_s == pytest.approx(25.0)
    assert _book_position(server) == pytest.approx(85.0)


async def test_contents_show_the_position_on_the_device_and_in_the_library(env, server):
    """the device page lists where each podcast episode / audiobook chapter stands."""
    app, c, mount = env
    rows = {r["key"]: r for r in (await c.get("/api/v1/devices/ipod/contents")).json()}
    assert rows[KEY]["position_s"] == pytest.approx(50.0)
    assert rows[KEY]["library_position_s"] == pytest.approx(50.0)
    assert rows["abs/b1:b1-2"]["position_s"] == pytest.approx(10.0)  # 10 s into chapter 2

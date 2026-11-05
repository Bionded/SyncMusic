"""Positions on a virtual iPod with a fake Audiobookshelf (docs/08 Positions): the bookmark the forward sync
writes from the library is recorded as the manifest's last-known position, so the next reverse sync does not push
it back over progress made on another client; a bookmark changed on the device is still pushed."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pygpod import Database as IPodDB

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.core import manifest as manifest_io
from syncmusic.core import models
from syncmusic.core import sync as sync_module
from syncmusic.facade import AppFacade
from syncmusic.libraries.audiobookshelf import AudiobookshelfLibrary
from tests.contracts.harness import ipod_tree
from tests.fake_abs import FakeABS
from tests.media import make_track
from tests.unit.test_api_sync import _apply, _register

PODS = {
    "rules": [
        {"id": "p", "name": "Spoken", "kind": ["podcast", "audiobook"], "criteria": {"all": []}}
    ]
}
KEY = "abs/p1:e1"


@pytest.fixture(autouse=True)
def chapter_files(monkeypatch):
    """These tests are about a book as one file per chapter on the iPod (what other devices get): no merge (the merged book: test_books.py)."""
    for module in (models, sync_module):
        monkeypatch.setattr(module, "COMBINE_AUDIOBOOKS_TYPES", frozenset())


@pytest.fixture
def server(tmp_path, monkeypatch):
    abs_ = FakeABS()
    abs_.add_podcast("p1", "Подкаст", [("e1", "Епізод 1")])
    data = make_track(tmp_path / "src" / "e1.m4a", seconds=3.0, title="Епізод 1").read_bytes()
    abs_.files[("p1", "p1-e1")] = data
    abs_.items["p1"]["media"]["episodes"][0]["audioFile"]["metadata"]["size"] = len(data)
    abs_.set_progress("p1", 50.0, episode="e1")  # listened to 50 s before the first sync
    abs_.add_book("b1", "Книга", [("01.mp3", 60.0), ("02.mp3", 30.0)])
    for i, item in enumerate(abs_.items["b1"]["media"]["tracks"], 1):
        chapter = make_track(tmp_path / "src" / f"b{i}.mp3", seconds=2.0, title=f"Розділ {i}")
        abs_.files[("b1", item["ino"])] = chapter.read_bytes()
        item["metadata"]["size"] = chapter.stat().st_size
    abs_.set_progress("b1", 70.0)  # 10 s into chapter 2
    monkeypatch.setattr(AudiobookshelfLibrary, "transport", abs_)
    return abs_


@pytest.fixture
async def env(config, server, tmp_path):
    app = Application(config)
    await app.start()
    api = create_api(AppFacade(app), manage_lifecycle=False)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
        r = await c.post(
            "/api/v1/libraries",
            json={
                "id": "abs",
                "type": "audiobookshelf",
                "name": "ABS",
                "settings": {"url": "http://abs.test", "api_token": "tok"},
            },
        )
        assert r.status_code == 201, r.text
        mount = ipod_tree(tmp_path / "ipod")
        await _register(c, mount, name="iPod", type="ipod", library_ids=["abs"], rule_set=PODS)
        await _sync(app, c, first_sync="keep")
        yield app, c, mount
    await app.stop()


async def _sync(app, c, **body):
    plan = (await c.post("/api/v1/devices/ipod/plan", json={})).json()
    return await _apply(app, c, plan["id"], **body)


def _episode(mount):
    dbid = manifest_io.read(mount).items[KEY].device_ref
    db = IPodDB(str(mount))
    return db, next(t for t in db.tracks if str(t.dbid) == dbid)


def _abs_position(server) -> float:
    return next(p for p in server.progress if p["episodeId"] == "e1")["currentTime"]


async def test_synced_bookmark_is_not_pushed_back(env, server):
    app, c, mount = env
    _, t = _episode(mount)
    assert t.bookmark_time == 50_000  # the library's position went to the device
    assert manifest_io.read(mount).items[KEY].last_position_s == pytest.approx(50.0)

    server.set_progress("p1", 70.0, episode="e1")  # 20 s more on the phone
    patches = len(server.patches)
    report = await _sync(app, c)  # nothing played on the iPod
    assert report["reverse"]["positions_pushed"] == 0
    assert _abs_position(server) == pytest.approx(70.0) and len(server.patches) == patches


async def test_bookmark_changed_on_the_device_is_pushed(env, server):
    app, c, mount = env
    db, t = _episode(mount)
    t.bookmark_time = 65_000  # listened on the iPod (not near the end: that is finished)
    db.save()
    report = await _sync(app, c)
    assert report["reverse"]["positions_pushed"] == 1
    assert _abs_position(server) == pytest.approx(65.0)
    assert manifest_io.read(mount).items[KEY].last_position_s == pytest.approx(65.0)
    report = await _sync(app, c)  # consumed: not pushed again
    assert report["reverse"]["positions_pushed"] == 0


def _book_position(server) -> float:
    return next(p for p in server.progress if p["libraryItemId"] == "b1")["currentTime"]


async def test_synced_chapter_bookmark_does_not_move_the_book_back(env, server):
    app, c, mount = env
    entry = manifest_io.read(mount).items["abs/b1:b1-2"]
    assert entry.last_position_s == pytest.approx(10.0)
    server.set_progress("b1", 85.0)  # further on another client
    report = await _sync(app, c)
    assert report["reverse"]["positions_pushed"] == 0
    assert _book_position(server) == pytest.approx(85.0)


async def test_audiobooks_in_progress_card(env, server, tmp_path):
    """the book on the device at 70 s (10 s into chapter 2) against the library's position now and
    the one recorded after the last sync; a flash drive (no bookmarks) lists nothing."""
    app, c, _ = env

    async def book():
        r = await c.get("/api/v1/devices/ipod/positions")
        assert r.status_code == 200, r.text
        (b,) = r.json()  # the podcast episode is not a book
        assert (
            b["title"] == "Книга"
            and b["key"] == "abs/b1:" + server.items["b1"]["media"]["tracks"][0]["ino"]
        )
        return b

    b = await book()
    assert b["position_s"] == 70.0 and b["duration_s"] == 90.0 and b["state"] == "in_sync"
    server.set_progress("b1", 20.0)  # moved back on another client since the sync
    b = await book()
    assert (b["state"], b["diff_s"], b["library_position_s"]) == ("library_ahead", 50.0, 20.0)
    await app.store.set_position_scopes("ipod", "abs", {"abs/b1": 20.0})  # as if recorded so
    b = await book()
    assert (b["state"], b["diff_s"]) == ("device_ahead", 50.0)

    await _register(c, tmp_path / "flash", rule_set={"rules": []}, library_ids=[])
    assert (await c.get("/api/v1/devices/my-flash/positions")).json() == []


def _abs_finished(server) -> bool:
    return next(p for p in server.progress if p["episodeId"] == "e1")["isFinished"]


async def test_episode_played_to_the_end_is_finished(env, server):
    """the iPod counts a play and keeps a ~6 s bookmark after the end: ABS gets finished, not 6 s;
    the bookmark left behind is not pushed by the next sync either."""
    app, c, mount = env
    db, t = _episode(mount)
    t.play_count += 1
    t.bookmark_time = 5_897
    db.save()
    report = await _sync(app, c)
    assert report["reverse"]["finished_pushed"] == 1
    assert _abs_finished(server)
    patches = len(server.patches)
    report = await _sync(app, c)  # nothing new on the iPod
    assert report["reverse"]["positions_pushed"] == 0 and len(server.patches) == patches
    assert _abs_finished(server)


async def test_bookmark_near_the_end_is_finished_and_relistening_is_a_position(env, server):
    app, c, mount = env
    db, t = _episode(mount)
    t.bookmark_time = 97_000  # 100 s episode: within 5 % of the end, no play counted yet
    db.save()
    report = await _sync(app, c)
    assert report["reverse"]["finished_pushed"] == 1 and _abs_finished(server)
    db, t = _episode(mount)
    t.play_count += 1
    t.bookmark_time = 65_000  # played again, past the restart margin, not near the end
    db.save()
    report = await _sync(app, c)
    assert report["reverse"]["finished_pushed"] == 0
    assert _abs_position(server) == pytest.approx(65.0) and not _abs_finished(server)


async def test_finished_survives_a_run_cancelled_before_the_device_commit(env, server, monkeypatch):
    """plays captured by a run that did not commit the device are found again; the episode stays
    finished instead of the reset bookmark following it."""
    from syncmusic.core.reverse import ReverseSync

    app, c, mount = env
    db, t = _episode(mount)
    t.play_count += 1
    t.bookmark_time = 5_897
    db.save()
    real = ReverseSync._commit

    async def fail(self, device, manifest, fb):
        raise RuntimeError("unplugged")

    monkeypatch.setattr(ReverseSync, "_commit", fail)
    report = await _sync(app, c)
    assert report["reverse"]["failed"]  # step 0 failed after capturing the plays
    monkeypatch.setattr(ReverseSync, "_commit", real)
    await _sync(app, c)
    assert _abs_finished(server)

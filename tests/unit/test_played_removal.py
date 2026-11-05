"""an episode played to the end on the iPod, on a device whose rule takes unplayed podcasts only, is
removed by the update that pushed it as finished - the update sends the listening data first, patches the cached
snapshot and plans after it - and a manual sync right after a cancelled update still sees it as played."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pygpod import Database as IPodDB

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import ItemRef
from syncmusic.facade import AppFacade
from syncmusic.libraries.audiobookshelf import AudiobookshelfLibrary
from tests.contracts.harness import ipod_tree
from tests.fake_abs import FakeABS
from tests.media import make_track
from tests.unit.test_api_sync import _apply, _register

UNPLAYED = {
    "rules": [
        {
            "id": "p",
            "name": "New episodes",
            "kind": ["podcast"],
            "criteria": {"is": {"finished": False}},
        }
    ]
}
KEY = "abs/p1:e1"


@pytest.fixture
def server(tmp_path, monkeypatch):
    abs_ = FakeABS()
    # six episodes: one removal stays under the 20 % delete threshold
    abs_.add_podcast("p1", "Подкаст", [(f"e{i}", f"Епізод {i}") for i in range(1, 7)])
    for i, ep in enumerate(abs_.items["p1"]["media"]["episodes"], 1):
        data = make_track(tmp_path / "src" / f"e{i}.m4a", seconds=3.0, title=f"Епізод {i}")
        abs_.files[("p1", f"p1-e{i}")] = data.read_bytes()
        ep["audioFile"]["metadata"]["size"] = len(abs_.files[("p1", f"p1-e{i}")])
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
        await _register(c, mount, name="iPod", type="ipod", library_ids=["abs"], rule_set=UNPLAYED)
        plan = (await c.post("/api/v1/devices/ipod/plan", json={})).json()
        await _apply(app, c, plan["id"], first_sync="keep")
        assert KEY in manifest_io.read(mount).items
        yield app, c, mount
    await app.stop()


def _play_to_the_end(mount):
    dbid = manifest_io.read(mount).items[KEY].device_ref
    db = IPodDB(str(mount))
    t = next(t for t in db.tracks if str(t.dbid) == dbid)
    t.play_count += 1
    t.bookmark_time = 5_897  # the iPod's bookmark after the end
    db.save()


def _finished(server) -> bool:
    return any(p["episodeId"] == "e1" and p["isFinished"] for p in server.progress)


async def _update(app, c):
    r = await c.post("/api/v1/devices/ipod/update")
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded", job.error
    return job.result


async def test_update_pushes_finished_and_removes_the_episode(env, server):
    app, c, mount = env
    _play_to_the_end(mount)
    report = await _update(app, c)
    assert report["reverse"]["finished_pushed"] == 1 and _finished(server)
    assert KEY not in manifest_io.read(mount).items
    assert "abs/p1:e2" in manifest_io.read(mount).items  # unplayed: stays
    assert not report["warnings"]
    assert "removed 1 played item" in report["message"]


async def test_update_keeps_it_when_the_setting_is_off(env, server):
    app, c, mount = env
    r = await c.patch("/api/v1/devices/ipod", json={"update_remove_played": False})
    assert r.status_code == 200, r.text
    _play_to_the_end(mount)
    await _update(app, c)
    assert _finished(server) and KEY in manifest_io.read(mount).items
    plan = (await c.post("/api/v1/devices/ipod/plan", json={})).json()
    removed = [a["ref"] for a in plan["actions"] if a["kind"] == "remove"]
    assert removed == [{"library_id": "abs", "item_id": "p1:e1"}]  # the full sync does it


async def test_the_delete_threshold_still_holds_it_back(env, server):
    app, c, mount = env
    r = await c.patch("/api/v1/devices/ipod", json={"delete_threshold_pct": 10})
    assert r.status_code == 200, r.text
    _play_to_the_end(mount)
    report = await _update(app, c)
    assert KEY in manifest_io.read(mount).items
    assert report["warnings"] == [
        "1 of 6 items to remove (more than 10 %) are left for a full sync to review"
    ]


async def test_manual_sync_after_a_cancelled_update_sees_the_episode_as_played(env, server):
    """The update pushed finished but stopped before the snapshot was expired: the cached snapshot was patched
    on delivery, so the full sync's plan (its own reverse sync finds nothing new) removes the episode."""
    app, c, _ = env
    await app.sync.snapshots.patch(
        "position",
        ItemRef(library_id="abs", item_id="p1:e1"),
        {"position_s": 3.0, "finished": True},
    )
    plan = (await c.post("/api/v1/devices/ipod/plan", json={})).json()
    removed = [a["ref"]["item_id"] for a in plan["actions"] if a["kind"] == "remove"]
    assert removed == ["p1:e1"]

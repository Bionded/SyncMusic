"""Rating conflicts and the forward-path overwrite (docs/08 Ratings) on a virtual iPod with Navidrome.

Testing-session scenario (note): rate on the device, change the title in the library, full sync (step 0 +
forward) → the library has the device's rating and the device keeps it. Both changed → the device wins.
rating_priority=library → the device's rating is replaced by the library's and never pushed.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pygpod import Database as IPodDB

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.core import manifest as manifest_io
from syncmusic.facade import AppFacade
from syncmusic.libraries.subsonic import SubsonicLibrary
from tests.contracts.harness import ipod_tree
from tests.fake_navidrome import FakeNavidrome, song
from tests.media import make_music
from tests.unit.test_api_sync import ALL, _apply, _register


@pytest.fixture
def server(tmp_path, monkeypatch):
    nd = FakeNavidrome(native=False)
    for sid, title in (("s1", "Група крові"), ("s2", "Зозуля")):
        path = make_music(tmp_path / "src" / f"{sid}.mp3", title=title, artist="Скрябін")
        s = song(sid, title)
        s["_data"] = path.read_bytes()
        s["size"] = len(s["_data"])
        nd.add(s)
    monkeypatch.setattr(SubsonicLibrary, "transport", nd)
    return nd


@pytest.fixture
async def env(config, server, tmp_path):
    app = Application(config)
    await app.start()
    api = create_api(AppFacade(app), manage_lifecycle=False)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
        r = await c.post(
            "/api/v1/libraries",
            json={
                "id": "nd",
                "type": "navidrome",
                "name": "Navidrome",
                "settings": {"url": "http://nd.test", "username": "admin", "password": "pw"},
            },
        )
        assert r.status_code == 201, r.text
        mount = ipod_tree(tmp_path / "ipod")
        await _register(c, mount, name="iPod", type="ipod", library_ids=["nd"], rule_set=ALL)
        await _sync(app, c, first_sync="keep")
        yield app, c, mount
    await app.stop()


async def _sync(app, c, **body):
    plan = (await c.post("/api/v1/devices/ipod/plan", json={})).json()
    return plan, await _apply(app, c, plan["id"], **body)


def _rate_on_device(mount, item_id: str, stars: int) -> None:
    dbid = manifest_io.read(mount).items[f"nd/{item_id}"].device_ref
    db = IPodDB(str(mount))
    next(t for t in db.tracks if str(t.dbid) == dbid).rating = stars * 20
    db.save()


def _on_device(mount, item_id: str):
    dbid = manifest_io.read(mount).items[f"nd/{item_id}"].device_ref
    return next(t for t in IPodDB(str(mount)).tracks if str(t.dbid) == dbid)


def _library_changed(server) -> None:
    server.last_scan = server.last_scan.replace("10:", "11:")  # a rescan: the state token changes


async def test_device_rating_survives_a_library_metadata_update(env, server):
    app, c, mount = env
    _rate_on_device(mount, "s1", 4)
    server.songs["s1"]["title"] = "Група крові (live)"
    _library_changed(server)
    plan, report = await _sync(app, c)
    assert plan["counts"]["update_meta"] == 1
    assert report["reverse"]["ratings_pushed"] == 1
    assert server.songs["s1"]["userRating"] == 4  # pushed to the library
    t = _on_device(mount, "s1")
    assert t.title == "Група крові (live)" and t.rating == 80  # the device keeps it
    assert manifest_io.read(mount).items["nd/s1"].last_rating == 4

    plan, report = await _sync(app, c)  # settled: nothing to push, nothing to update
    assert plan["counts"]["update_meta"] == 0 and report["reverse"]["ratings_pushed"] == 0
    assert _on_device(mount, "s1").rating == 80


async def test_both_changed_device_wins(env, server):
    app, c, mount = env
    _rate_on_device(mount, "s2", 2)
    server.songs["s2"]["userRating"] = 5
    _library_changed(server)
    plan, report = await _sync(app, c)
    assert server.songs["s2"]["userRating"] == 2 and _on_device(mount, "s2").rating == 40
    plan, report = await _sync(app, c)
    assert plan["counts"]["update_meta"] == 0 and _on_device(mount, "s2").rating == 40


async def test_library_only_change_reaches_the_device(env, server):
    app, c, mount = env
    server.songs["s1"]["userRating"] = 3
    _library_changed(server)
    plan, report = await _sync(app, c)
    assert report["reverse"]["ratings_pushed"] == 0
    assert _on_device(mount, "s1").rating == 60 and server.songs["s1"]["userRating"] == 3


async def test_library_priority_replaces_the_device_rating(env, server):
    app, c, mount = env
    r = await c.patch("/api/v1/devices/ipod", json={"sync": {"rating_priority": "library"}})
    assert r.status_code == 200, r.text
    server.songs["s1"]["userRating"] = 2
    _library_changed(server)
    await _sync(app, c)  # library value on the device
    calls = len(server.calls)
    _rate_on_device(mount, "s1", 5)
    plan, report = await _sync(app, c)
    assert plan["counts"]["update_meta"] == 0  # planned as KEEP, rewritten at apply time
    assert report["reverse"]["ratings_pushed"] == 0
    assert "setRating" not in server.calls[calls:] and server.songs["s1"]["userRating"] == 2
    assert _on_device(mount, "s1").rating == 40  # the device's 5 stars replaced
    assert manifest_io.read(mount).items["nd/s1"].last_rating == 2


async def test_degraded_navidrome_snapshot_is_not_planned_on(env, server, monkeypatch):
    """the native API refusing logins (429) makes the plan fail, not use a degraded snapshot."""
    app, c, mount = env
    monkeypatch.setattr(SubsonicLibrary, "native_retry_s", (0,))
    server.native, server.refuse_logins = True, 99
    app.sync.forget_library("nd")
    _library_changed(server)
    r = await c.post("/api/v1/devices/ipod/plan", json={})
    assert r.status_code == 503 and r.json()["error"] == "library_unavailable", r.text
    assert "429" in r.json()["message"]


async def test_failed_step0_does_not_overwrite_the_device_rating(env, server, monkeypatch):
    """Step 0 raises (unreadable Play Counts, I/O error) and the forward sync continues: the forward
    UPDATE_META must not write the stale library rating over the device's unpulled rating, which would lose
    it for good (the next reverse sync then sees no change)."""
    from syncmusic.core.reverse import ReverseSync

    app, c, mount = env
    _rate_on_device(mount, "s1", 4)
    server.songs["s1"]["title"] = "Група крові (live)"
    _library_changed(server)

    async def unreadable(self, *a, **kw):
        raise OSError(5, "Input/output error")

    run = ReverseSync.run
    monkeypatch.setattr(ReverseSync, "run", unreadable)
    plan, report = await _sync(app, c)
    assert report["reverse"]["errors"] and report["reverse"]["failed"]
    assert report["counts"]["update_meta"] == 0  # deferred
    t = _on_device(mount, "s1")
    assert t.rating == 80 and t.title == "Група крові"
    monkeypatch.setattr(ReverseSync, "run", run)
    plan, report = await _sync(app, c)  # step 0 works again: the deferred update is planned again
    assert plan["counts"]["update_meta"] == 1 and report["reverse"]["ratings_pushed"] == 1
    t = _on_device(mount, "s1")
    assert t.rating == 80 and t.title == "Група крові (live)"
    assert server.songs["s1"]["userRating"] == 4


async def test_failed_step0_does_not_lose_ipod_plays(env, server, monkeypatch):
    """Step 0 raises; the forward sync saves the iPod database, which merges and deletes Play Counts. The plays
    must still reach the library on the next working sync (exactly once)."""
    from syncmusic.core.reverse import ReverseSync
    from tests.contracts.harness import _ipod_feedback

    app, c, mount = env
    device = await app.sync.open_device("ipod")
    await _ipod_feedback(device, manifest_io.read(mount))  # 2 plays + 4 stars on every track
    server.songs["s1"]["title"] = "Група крові (live)"  # the forward sync writes the database
    _library_changed(server)

    async def unreadable(self, *a, **kw):
        raise OSError(5, "Input/output error")

    run = ReverseSync.run
    monkeypatch.setattr(ReverseSync, "run", unreadable)
    await _sync(app, c)
    monkeypatch.setattr(ReverseSync, "run", run)
    _, report = await _sync(app, c)
    assert report["reverse"]["plays_submitted"] == 4, report["reverse"]


async def test_rating_cleared_on_the_device_clears_the_library(env, server):
    app, c, mount = env
    server.songs["s1"]["userRating"] = 5
    _library_changed(server)
    await _sync(app, c)
    assert _on_device(mount, "s1").rating == 100
    _rate_on_device(mount, "s1", 0)
    _, report = await _sync(app, c)
    assert report["reverse"]["ratings_pushed"] == 1
    assert not server.songs["s1"].get("userRating") and _on_device(mount, "s1").rating == 0


def test_defer_device_rewrites_only_after_a_failed_step0():
    from syncmusic.core.models import ActionKind, ItemRef, PlanAction, ReverseReport, SyncPlan
    from syncmusic.core.plan_adjust import defer_device_rewrites

    def act(kind, item_id):
        return PlanAction(
            kind=kind, ref=ItemRef(library_id="nd", item_id=item_id), meta_hash="new", size_delta=5
        )

    kinds = [ActionKind.ADD, ActionKind.REPLACE, ActionKind.UPDATE_META, ActionKind.REMOVE]
    plan = SyncPlan(id="p", device_id="d", actions=[act(k, str(i)) for i, k in enumerate(kinds)])
    ok = ReverseReport()
    assert defer_device_rewrites(plan, ok, True) is plan and ok.errors == []
    failed = ReverseReport(failed=True)
    assert defer_device_rewrites(plan, failed, False) is plan  # no ratings written (Rockbox, flash)
    out = defer_device_rewrites(plan, failed, True)
    assert [a.kind for a in out.actions] == [
        ActionKind.ADD,
        ActionKind.KEEP,
        ActionKind.KEEP,
        ActionKind.REMOVE,
    ]
    assert [a.meta_hash for a in out.actions[1:3]] == ["", ""]  # the manifest keeps its old hash
    assert plan.actions[1].kind is ActionKind.REPLACE  # the input plan is not changed
    assert "deferred" in failed.errors[0]


async def test_failed_ipod_commit_after_capture_does_not_duplicate_plays(env, server, monkeypatch):
    """Update job: Play Counts read and captured, then writing the iPod fails (unplugged). The Play Counts stay on
    the device and are read again with re-estimated times; the plays reach Navidrome exactly once."""
    from syncmusic.devices.iPod.ipod import IPod
    from tests.contracts.harness import _ipod_feedback

    app, c, mount = env
    device = await app.sync.open_device("ipod")
    await _ipod_feedback(device, manifest_io.read(mount))  # 2 plays on every track
    before = {sid: int(s.get("playCount") or 0) for sid, s in server.songs.items()}

    async def unplugged(self, s, manifest):
        raise OSError(5, "Input/output error")

    real = IPod.commit
    monkeypatch.setattr(IPod, "commit", unplugged)
    r = await c.post("/api/v1/devices/ipod/update")
    assert r.status_code == 202, r.text
    assert (await app.jobs.wait(r.json()["id"])).status == "failed"
    assert all(int(s.get("playCount") or 0) == before[sid] for sid, s in server.songs.items())
    monkeypatch.setattr(IPod, "commit", real)
    app.sync._damaged.pop("ipod")  # the failed write marked it damaged; a repair clears that
    for _ in range(2):  # the second run finds nothing new
        r = await c.post("/api/v1/devices/ipod/update")
        job = await app.jobs.wait(r.json()["id"])
        assert job.status == "succeeded", job.error
    assert {sid: int(s.get("playCount") or 0) for sid, s in server.songs.items()} == {
        sid: n + 2 for sid, n in before.items()
    }

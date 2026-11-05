"""Reverse sync through the service and the API: step 0 of apply and of an update (the standalone `POST /devices/{id}/reverse` went)."""

from __future__ import annotations

from syncmusic.core import manifest as manifest_io
from syncmusic.core.applier import Applier
from syncmusic.core.reverse import ReverseSync
from tests.contracts.harness import _rockbox_feedback, rockbox_tree
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401 - fixtures


async def _rockbox(app, c, tmp_path):
    mount = rockbox_tree(tmp_path / "rockbox")
    await _register(c, mount, name="Mini", type="rockbox")
    plan = (await c.post("/api/v1/devices/mini/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    device = await app.sync.open_device("mini")
    return mount, device


async def test_update_job_sends_back_and_reports(env, tmp_path):  # noqa: F811
    app, c = env
    mount, device = await _rockbox(app, c, tmp_path)
    manifest = manifest_io.read(mount)
    await _rockbox_feedback(device, manifest)  # one full play of each of the 3 tracks

    r = await c.post("/api/v1/devices/mini/update")
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded", job.error
    rev = job.result["reverse"]
    assert rev["plays_found"] == 3 and rev["pending"] == 0 and job.result["status"] == "succeeded"
    # folder libraries get no reverse sync: the plays are consumed from the device, not stored
    for key in manifest.items:
        lib_id, _, item_id = key.partition("/")
        assert await app.sync.store.local_feedback(lib_id, item_id) is None
    assert all(e.last_playcount == 1 for e in manifest_io.read(mount).items.values())
    assert not (mount / ".rockbox" / "playback.log").exists()  # rotated after the commit
    reports = (await c.get("/api/v1/sync-reports", params={"device": "mini"})).json()
    assert reports[0]["reverse"]["plays_found"] == 3
    # an update: the Overview's "Last full sync" is the apply's report
    assert reports[0]["kind"] == "update" and reports[1]["kind"] == "full"
    assert (await c.get("/api/v1/devices/mini")).json()["last_report"]["id"] == reports[1]["id"]

    # read again: nothing new
    job = await app.jobs.wait((await c.post("/api/v1/devices/mini/update")).json()["id"])
    assert job.result["reverse"]["plays_found"] == 0
    assert (await c.post("/api/v1/devices/nope/update")).status_code == 404


async def test_apply_runs_reverse_first(env, tmp_path, monkeypatch):  # noqa: F811
    app, c = env
    mount, device = await _rockbox(app, c, tmp_path)
    await _rockbox_feedback(device, manifest_io.read(mount))
    order: list[str] = []
    run, apply = ReverseSync.run, Applier.apply

    async def rev(self, *a, **kw):
        order.append("reverse")
        return await run(self, *a, **kw)

    async def fwd(self, *a, **kw):
        order.append("apply")
        return await apply(self, *a, **kw)

    monkeypatch.setattr(ReverseSync, "run", rev)
    monkeypatch.setattr(Applier, "apply", fwd)
    plan = (await c.post("/api/v1/devices/mini/plan", json={})).json()
    report = await _apply(app, c, plan["id"])
    assert order == ["reverse", "apply"]
    assert report["reverse"]["plays_found"] == 3 and report["status"] == "succeeded"


async def test_reverse_failure_does_not_stop_the_sync(env, tmp_path, monkeypatch):  # noqa: F811
    app, c = env
    await _rockbox(app, c, tmp_path)

    async def boom(self, *a, **kw):
        raise RuntimeError("sink exploded")

    monkeypatch.setattr(ReverseSync, "run", boom)
    plan = (await c.post("/api/v1/devices/mini/plan", json={})).json()
    report = await _apply(app, c, plan["id"])
    assert report["status"] == "succeeded"
    assert report["reverse"]["errors"] == ["reverse sync failed: sink exploded"]


async def test_folder_library_never_overwrites_the_device_rating(env, music, tmp_path):  # noqa: F811
    """a library that cannot store ratings (Folder) never replaces the device's rating, also when the
    file changes in the library (REPLACE); folder libraries get no reverse sync, nothing is stored."""
    from pygpod import Database as IPodDB

    from tests.contracts.harness import ipod_tree
    from tests.media import make_music

    app, c = env
    mount = ipod_tree(tmp_path / "ipod")
    await _register(c, mount, name="iPod", type="ipod")
    plan = (await c.post("/api/v1/devices/ipod/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    key, entry = next(iter(manifest_io.read(mount).items.items()))
    db = IPodDB(str(mount))
    next(t for t in db.tracks if str(t.dbid) == entry.device_ref).rating = 80
    db.save()
    lib_id, _, item_id = key.partition("/")
    tracks = (await app.sync.snapshots.get(await app.sync.library(lib_id))).tracks
    src = music / tracks[item_id].path
    make_music(src, title="Нова версія", artist="Скрябін", album="Група крові", seconds=4.0)

    plan = (await c.post("/api/v1/devices/ipod/plan", json={})).json()
    assert plan["counts"]["replace"] == 1
    report = await _apply(app, c, plan["id"])
    assert report["reverse"]["ratings_pushed"] == 0  # not a sink
    assert await app.sync.store.local_feedback(lib_id, item_id) is None
    new = manifest_io.read(mount).items[key]
    t = next(t for t in IPodDB(str(mount)).tracks if str(t.dbid) == new.device_ref)
    assert t.title == "Нова версія" and t.rating == 80 and new.last_rating == 4


async def test_audiobook_folder_library_goes_to_the_ipod_audiobooks(env, tmp_path):  # noqa: F811
    """a folder library of type audiobooks lands in the iPod's Audiobooks menu (media type audiobook,
    remembers the position), music in Music."""
    from pygpod import Database as IPodDB
    from pygpod.db.constants import MEDIATYPE_AUDIOBOOK

    from tests.contracts.harness import ipod_tree
    from tests.media import make_music

    app, c = env
    books = tmp_path / "books"
    for n in (1, 2):
        make_music(
            books / "Тигролови" / f"0{n}.mp3",
            title=f"Розділ {n}",
            album="Тигролови",
        )
    r = await c.post(
        "/api/v1/libraries",
        json={
            "id": "books",
            "type": "folder",
            "name": "Books",
            "settings": {"root": str(books), "default_kind": "audiobook"},
        },
    )
    assert r.status_code == 201, r.text
    mount = ipod_tree(tmp_path / "ipod")
    await _register(c, mount, name="iPod", type="ipod", library_ids=["music", "books"])
    plan = (await c.post("/api/v1/devices/ipod/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    by_title = {t.title: t for t in IPodDB(str(mount)).tracks}
    t = by_title["Тигролови"]  # both chapters merged into one chaptered book
    assert t.media_type & MEDIATYPE_AUDIOBOOK and t.remember_position
    assert "Розділ 1" not in by_title
    assert not by_title["Група крові"].media_type & MEDIATYPE_AUDIOBOOK

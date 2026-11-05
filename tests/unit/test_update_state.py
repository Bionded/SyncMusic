"""What an update changed and the update line's state: counts per kind in the report, and `update` in
GET /devices and GET /devices/{id} - off, none, running, paused, ok, skipped, failed."""

# ruff: noqa: F811 - fixtures imported from test_api_sync / test_applier

from __future__ import annotations

import asyncio

from syncmusic.core.models import SyncReport
from syncmusic.core.plan_adjust import update_summary
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401
from tests.unit.test_applier import _flash, _sync, library, preparer  # noqa: F401


async def test_playlists_count_only_when_they_change(tmp_path, library, preparer):
    dev = _flash(tmp_path)
    _, first = await _sync(dev, library, preparer)
    assert first.counts["playlists"] == 1  # "Все" is new
    assert list(first.playlists_written) == ["Все"] and first.playlists_written["Все"] > 0
    _, again = await _sync(dev, library, preparer)
    assert again.counts["playlists"] == 0  # the same tracks: not counted
    assert again.playlists_written == {} and again.playlists_dropped == []
    (tmp_path / "lib" / "Два.mp3").unlink()
    _, changed = await _sync(dev, library, preparer)
    assert (
        changed.counts["playlists"] == 1
    )  # the deleted track is an orphan: kept, but not in the playlist
    manifest = await dev.read_manifest()
    assert set(manifest.playlist_hashes) == {"Все"}


def test_summary_names_only_what_changed():
    r = SyncReport(
        id="r",
        device_id="d",
        kind="update",
        counts={
            "update_meta": 2,
            "replace": 1,
            "ratings": 3,
            "positions": 0,
            "playlists": 2,
            "keep": 9,
        },
    )
    assert update_summary(r) == {"tags": 3, "ratings": 3, "playlists": 2}


async def _update_of(c):
    one = (await c.get("/api/v1/devices/flash")).json()["update"]
    listed = (await c.get("/api/v1/devices")).json()["registered"][0]["update"]
    assert one == listed
    return one


async def test_update_line_states(env, music, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    assert (await _update_of(c))["state"] == "none"

    r = await c.post("/api/v1/devices/flash/update")  # before the first full sync
    await app.jobs.wait(r.json()["id"])
    u = await _update_of(c)
    assert u["state"] == "skipped" and "first full sync" in u["error"] and u["last_at"]

    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    events = app.bus.subscribe(256)
    r = await c.post("/api/v1/devices/flash/update")
    await app.jobs.wait(r.json()["id"])
    u = await _update_of(c)
    assert u["state"] == "ok" and u["error"] is None and isinstance(u["summary"], dict)
    seen = [events.get_nowait() for _ in range(events.qsize())]
    started = [e for e in seen if e.type == "update.started"]
    assert started and started[0].data["device_id"] == "flash"

    # running while the update job is on the device; paused while a full sync holds it
    gate = asyncio.Event()

    async def held(ctx):
        await gate.wait()
        return {}

    job = app.jobs.submit("update", "update Flash", held, device_id="flash")
    await asyncio.sleep(0)
    assert (await _update_of(c))["state"] == "running"
    gate.set()
    await app.jobs.wait(job.id)
    gate.clear()
    job = app.jobs.submit("sync", "sync Flash", held, device_id="flash")
    await asyncio.sleep(0)
    assert (await _update_of(c))["state"] == "paused"
    gate.set()
    await app.jobs.wait(job.id)

    r = await c.patch("/api/v1/devices/flash", json={"update_sync": False})
    assert r.status_code == 200, r.text
    assert (await _update_of(c))["state"] == "off"


async def test_a_failed_update_says_why(env, music, tmp_path, monkeypatch):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")

    async def broken(*a, **kw):
        raise RuntimeError("the library is down")

    monkeypatch.setattr(app.sync, "_step0", broken)
    r = await c.post("/api/v1/devices/flash/update")
    await app.jobs.wait(r.json()["id"])
    u = await _update_of(c)
    assert u["state"] == "failed" and "the library is down" in u["error"]


async def test_a_full_sync_supersedes_a_skipped_update(env, music, tmp_path):
    """Connect → the on-connect update is skipped (no full sync yet) → the user syncs: the update line must not keep
    saying 'skipped … no update before the first full sync'."""
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    r = await c.post("/api/v1/devices/flash/update")
    await app.jobs.wait(r.json()["id"])
    assert (await _update_of(c))["state"] == "skipped"
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    u = await _update_of(c)
    assert u["state"] in ("none", "ok"), u
    assert u["error"] is None and u["summary"] == {} and u["last_at"]


async def test_a_full_sync_supersedes_a_failed_update_but_not_a_newer_one(
    env, music, tmp_path, monkeypatch
):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")

    async def broken(*a, **kw):
        raise RuntimeError("the library is down")

    real = app.sync._step0
    monkeypatch.setattr(app.sync, "_step0", broken)
    r = await c.post("/api/v1/devices/flash/update")
    await app.jobs.wait(r.json()["id"])
    assert (await _update_of(c))["state"] == "failed"

    monkeypatch.setattr(app.sync, "_step0", real)
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"])
    assert (await _update_of(c))["state"] == "ok"

    monkeypatch.setattr(app.sync, "_step0", broken)
    r = await c.post("/api/v1/devices/flash/update")
    await app.jobs.wait(r.json()["id"])
    u = await _update_of(c)
    assert u["state"] == "failed" and "the library is down" in u["error"]


async def test_smart_playlist_reordered_is_not_logged(tmp_path, library, preparer):
    """a smart playlist whose order differs on every evaluation is rewritten, but not logged as
    written while its songs stay the same; a rule playlist's new order is."""
    from syncmusic.core.applier import Applier
    from syncmusic.core.prepare import Chain
    from tests.unit.test_applier import _plan

    dev = _flash(tmp_path)
    await _sync(dev, library, preparer)

    async def reordered(smart: bool, order=reversed):
        plan = await _plan(dev, library)
        plan.playlists = [
            p.model_copy(update={"refs": list(order(p.refs)), "smart": smart})
            for p in plan.playlists
        ]
        return await Applier(dev, {"lib": library}, Chain([]), preparer).apply(plan)

    assert (await reordered(True)).playlists_written == {}
    assert list((await reordered(False, order=list)).playlists_written) == ["Все"]

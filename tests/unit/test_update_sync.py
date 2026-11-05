"""Update sync: metadata, ratings, positions and playlists change on the device, new files
wait for a full sync, removals only when the device allows them, no review."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import pytest
from mutagen.easyid3 import EasyID3

from syncmusic.core.detection import ConnectedDisk
from syncmusic.core.models import (
    ActionKind,
    ItemRef,
    ManifestEntry,
    PlanAction,
    ReverseReport,
    SyncPlan,
    Track,
)
from syncmusic.core.plan_adjust import update_only, update_writes_items
from syncmusic.core.planner import initial_plan_item
from syncmusic.core.sync import SyncError
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.media import make_music, make_video
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401


def _action(kind: ActionKind, n: int) -> PlanAction:
    return PlanAction(
        kind=kind, ref=ItemRef(library_id="m", item_id=str(n)), size_delta=10, meta_hash="h"
    )


def test_update_only_keeps_what_needs_no_new_file():
    kinds = [
        ActionKind.ADD,
        ActionKind.REPLACE,
        ActionKind.UPDATE_META,
        ActionKind.REMOVE,
        ActionKind.KEEP,
    ]
    plan = SyncPlan(
        id="p",
        device_id="d",
        actions=[_action(k, i) for i, k in enumerate(kinds)],
        does_not_fit=True,
    )
    out = update_only(plan)
    assert [(a.ref.item_id, a.kind) for a in out.actions] == [
        ("1", ActionKind.KEEP),  # the replacement file waits; its hash does not advance
        ("2", ActionKind.UPDATE_META),
        ("4", ActionKind.KEEP),
    ]
    assert out.actions[0].meta_hash == "" and out.actions[0].size_delta == 0
    assert not out.does_not_fit
    assert [a.kind for a in plan.actions] == kinds  # the original is untouched
    with_removals = update_only(plan, removals=True)
    assert ActionKind.REMOVE in [a.kind for a in with_removals.actions]


async def _job(app, r):
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded", job.error
    return job.result


def _files(root):
    return sorted(p.name for p in root.rglob("*.mp3"))


async def test_update_sync_copies_rewrites_and_removes_nothing(env, music, tmp_path):
    app, c = env
    flash = tmp_path / "flash"
    await _register(c, flash, name="Flash")
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    before = _files(flash)
    assert len(before) == 3

    # the library changes: a new track, a retagged one, one gone
    make_music(
        music / "Тартак" / "Виходу немає.mp3",
        title="Виходу немає",
        artist="Тартак",
        album="Гранатовий альбом",
    )
    tags = EasyID3(music / "Тартак" / "Орбіт.mp3")
    tags["title"] = "Орбіт без цукру (2024)"
    tags.save()
    (music / "Скрябін" / "02 Зачини за мною двері.mp3").unlink()
    await _job(app, await c.post("/api/v1/libraries/music/refresh"))

    report = await _job(app, await c.post("/api/v1/devices/flash/update"))
    assert report["kind"] == "update" and report["status"] == "succeeded"
    counts = report["counts"]
    assert counts["add"] == counts["replace"] == counts["remove"] == 0 and counts["keep"] == 2
    # the deleted one would go, but 1 of 3 is over the delete threshold: held for the full sync
    assert report["warnings"] == [
        "1 of 3 items to remove (more than 20 %) are left for a full sync to review"
    ]
    assert _files(flash) == before  # nothing new, nothing removed
    # a folder library's retag changed the source file: rewriting it is a full sync's job
    synced = [p for p in flash.rglob("*.mp3") if "Орбіт" in p.name]
    assert synced and EasyID3(synced[0])["title"] == ["Орбіт без цукру"]
    assert not app.sync.plans  # nothing offered for review

    # the full sync still brings the rest
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    assert plan["counts"]["add"] == 1 and plan["counts"]["replace"] == 1


async def test_update_sync_before_the_first_full_sync_is_skipped(env, tmp_path):
    app, c = env
    flash = tmp_path / "flash"
    await _register(c, flash, name="Flash")
    report = await _job(app, await c.post("/api/v1/devices/flash/update"))
    assert report["kind"] == "update" and "first full sync" in report["message"]
    assert report["status"] == "skipped"  # not a green success in History
    assert _files(flash) == []


async def test_update_sync_without_templates_is_skipped_quietly(env, tmp_path):
    """a library refresh starts updates; a device with no templates has nothing to update from - a quiet
    skip, not a failed job and a 'Update failed' notification."""
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    r = await c.patch("/api/v1/devices/flash", json={"rule_set": {"template_ids": []}})
    assert r.status_code == 200, r.text
    result = await _job(app, await c.post("/api/v1/devices/flash/update"))
    assert result["status"] == "skipped" and "no templates" in result["message"]
    assert not [n for n in await app.store.notifications() if "Update failed" in str(n)]


async def _settle(app):
    for _ in range(200):
        if not app.sync._update_tasks and not app.jobs.device_busy("flash"):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("update sync still running")


async def test_periodic_checks_run_when_due_or_changed_and_skip_when_nothing_changed(
    env, music, tmp_path, monkeypatch
):
    app, c = env
    # the host just booted: its monotonic clock is behind the test clock below
    monkeypatch.setattr("syncmusic.core.sync.time", SimpleNamespace(monotonic=lambda: 300.0))
    monkeypatch.setattr(
        "syncmusic.core.update_status.time", SimpleNamespace(monotonic=lambda: 300.0)
    )
    await _register(c, tmp_path / "flash", name="Flash")
    await c.patch("/api/v1/devices/flash", json={"update_every_min": 15})  # new devices: 60
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    reports = len((await c.get("/api/v1/sync-reports?device=flash")).json())

    assert await app.sync.periodic_updates(now=1000.0) == []  # first seen: the interval starts
    assert await app.sync.periodic_updates(now=1000.0 + 14 * 60) == []
    assert await app.sync.periodic_updates(now=1000.0 + 15 * 60) == [
        "flash"
    ]  # every 15 min by default
    await _settle(app)
    after_first = len((await c.get("/api/v1/sync-reports?device=flash")).json())
    assert after_first == reports + 1  # the first check writes (nothing known yet)
    # its reverse sync pushed nothing: the snapshot stays valid (expiring it reloaded it a minute later and
    # marked every device for the next update, forever)
    assert not app.sync.snapshots._expired

    # a library change marks the device: checked in its next slot, at most once per `update_change_gap_min`
    # (15 min) - the interval is longer now, so only the change starts it; a new track is not an update →
    # nothing written
    r = await c.patch("/api/v1/devices/flash", json={"update_every_min": 60})
    assert r.status_code == 200, r.text
    make_music(
        music / "Тартак" / "Виходу немає.mp3",
        title="Виходу немає",
        artist="Тартак",
        album="Гранатовий альбом",
    )
    await _job(app, await c.post("/api/v1/libraries/music/refresh"))
    await _settle(app)
    assert (
        await app.sync.periodic_updates(now=1000.0 + 16 * 60) == []
    )  # the change waits for its slot
    # ... and the device card says when: its last update + 15 min (the fake monotonic clock is at 300)
    slot = app.sync._update_ran["flash"] + 15 * 60 - 300
    devs = (await c.get("/api/v1/devices")).json()["registered"]
    change_at = next(d for d in devs if d["id"] == "flash")["update"]["change_at"]
    left = dt.datetime.fromisoformat(change_at) - dt.datetime.now(dt.UTC)
    assert dt.timedelta(seconds=slot - 10) < left <= dt.timedelta(seconds=slot)
    assert await app.sync.periodic_updates(now=1000.0 + 29 * 60) == []
    assert await app.sync.periodic_updates(now=1000.0 + 30 * 60) == ["flash"]
    await _settle(app)
    assert len((await c.get("/api/v1/sync-reports?device=flash")).json()) == after_first
    devs = (await c.get("/api/v1/devices")).json()["registered"]
    assert next(d for d in devs if d["id"] == "flash")["update"]["change_at"] is None
    assert await app.sync.periodic_updates(now=1000.0 + 31 * 60) == []

    # switched off
    r = await c.patch("/api/v1/devices/flash", json={"update_sync": False})
    assert r.status_code == 200, r.text
    assert await app.sync.periodic_updates(now=1000.0 + 60 * 60) == []


async def test_connect_runs_an_update_sync_and_copies_no_new_file(env, music, tmp_path):
    app, c = env
    q = app.bus.subscribe()
    cfg = await _register(c, tmp_path / "flash", name="Flash")
    assert (
        cfg["on_connect"] == "manual" and cfg["update_sync"]
    )  # new devices: Sync copies new files
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    before = _files(tmp_path / "flash")
    make_music(music / "Тартак" / "Нова.mp3", title="Нова", artist="Тартак", album="Новий")
    await _job(app, await c.post("/api/v1/libraries/music/refresh"))
    while not q.empty():
        q.get_nowait()

    disk = BlockDeviceInfo(name="sdz", path="/dev/sdz")
    await app.sync.on_disk_added(ConnectedDisk(disk=disk, device_id="flash"))
    await _settle(app)
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert not any(
        e.type == "sync.needs_confirmation" for e in events
    )  # manual: nothing to confirm
    assert any(e.type == "update.finished" for e in events)
    assert app.jobs.jobs()[0].kind == "update"
    assert _files(tmp_path / "flash") == before  # the new track waits for Sync

    # a full automatic sync on connect does the update's work itself: no second job
    await c.patch("/api/v1/devices/flash", json={"on_connect": "auto"})
    jobs = len(app.jobs.jobs())
    await app.sync.on_disk_added(ConnectedDisk(disk=disk, device_id="flash"))
    new = app.jobs.jobs()[: len(app.jobs.jobs()) - jobs]
    assert [j.kind for j in new] == ["sync"]
    await app.jobs.wait(new[0].id)
    assert "Нова.mp3" in _files(tmp_path / "flash")


async def test_listening_data_from_one_device_marks_the_others(env, tmp_path, monkeypatch):
    app, c = env
    await _register(c, tmp_path / "a", name="A")
    await _register(c, tmp_path / "b", name="B")
    await _register(c, tmp_path / "c", name="C", library_ids=[], rule_set={"rules": []})

    async def run(cfg, device, libraries, unavailable=None, playlist_choices=None):
        return ReverseReport(plays_submitted=1)

    monkeypatch.setattr(app.sync.reverse_sync, "run", run)
    a = await app.sync.store.device("a")
    await app.sync._run_reverse(a, None)
    assert app.sync._update_dirty == {"b"}  # not the reporting device, not one without the library

    app.sync._update_dirty.clear()

    async def nothing(cfg, device, libraries, unavailable=None, playlist_choices=None):
        return ReverseReport(plays_found=2, plays_duplicate=2)

    monkeypatch.setattr(app.sync.reverse_sync, "run", nothing)
    await app.sync._run_reverse(a, None)
    assert app.sync._update_dirty == set()  # nothing reached the libraries


def test_update_only_keeps_a_tags_only_rewrite():
    ref = ItemRef(library_id="m", item_id="1")
    entry = ManifestEntry(
        ref=ref, device_ref="a.mp3", path="a.mp3", size=10, source_state="s1", chain_hash="c1"
    )
    item = initial_plan_item(Track(ref=ref, title="A", source_state="s1"))
    tags = _action(ActionKind.REPLACE, 1).model_copy(
        update={"entry": entry, "item": item, "chain_hash": "c1"}
    )
    source = tags.model_copy(update={"chain_hash": "c2"})  # processing changed: a new file
    out = update_only(SyncPlan(id="p", device_id="d", actions=[tags, source]))
    assert [a.kind for a in out.actions] == [ActionKind.REPLACE, ActionKind.KEEP]
    assert update_writes_items(out)
    assert not update_writes_items(SyncPlan(id="p", device_id="d", actions=[out.actions[1]]))


async def test_update_sync_rewrites_tags_in_place_on_a_file_device(env, music, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")  # flash drives get the tag step
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    manifest = tmp_path / "flash" / ".syncmusic" / "manifest.jsonl"
    lines = [json.loads(x) for x in manifest.read_text().splitlines()]
    stale = next(x for x in lines if x.get("meta_hash"))
    stale["meta_hash"] = (
        "stale"  # the library's tags changed, the source file and processing did not
    )
    manifest.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines))
    before = _files(tmp_path / "flash")

    report = await _job(app, await c.post("/api/v1/devices/flash/update"))
    assert report["counts"]["replace"] == 1 and report["counts"]["add"] == 0
    assert _files(tmp_path / "flash") == before
    report = await _job(app, await c.post("/api/v1/devices/flash/update"))
    assert report["counts"]["replace"] == 0  # written: the manifest has the new tags


async def test_device_page_data_separates_full_and_update_syncs(env, music, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    await c.patch("/api/v1/devices/flash", json={"update_every_min": 15})  # new devices: 60
    info = (await c.get("/api/v1/devices/flash")).json()
    assert info["last_report"] is None and info["last_update"] is None
    assert info["next_update_check"] is None  # no check scheduled yet
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    await _job(app, await c.post("/api/v1/devices/flash/update"))
    await app.sync.periodic_updates()  # first seen: the interval starts now

    info = (await c.get("/api/v1/devices/flash")).json()
    assert info["last_report"]["kind"] == "full" and info["last_update"]["kind"] == "update"
    left = dt.datetime.fromisoformat(info["next_update_check"]) - dt.datetime.now(dt.UTC)
    assert dt.timedelta(minutes=14) < left <= dt.timedelta(minutes=15)
    await c.patch("/api/v1/devices/flash", json={"update_every_min": None})
    assert (await c.get("/api/v1/devices/flash")).json()["next_update_check"] is None

    types = (await c.get("/api/v1/device-types")).json()
    general = next(t for t in types if t["type"] == "flashdrive")["general_schema"]["properties"]
    assert list(general)[:5] == [
        "update_sync",
        "update_every_min",
        "update_removals",
        "update_remove_played",
        "on_connect",
    ]


async def test_the_full_sync_delete_threshold_does_not_block_an_update(env, music, tmp_path):
    """An update without removals removes nothing, so the full sync's delete threshold must not
    skip it - the tag rewrite (and playlists, ratings, positions) still happen; the excluded item stays."""
    app, c = env
    flash = tmp_path / "flash"
    await _register(c, flash, name="Flash")
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    before = _files(flash)
    cfg = (await c.get("/api/v1/devices/flash")).json()["config"]
    assert not cfg["update_removals"] and cfg["delete_threshold_pct"] == 20
    tid = cfg["rule_set"]["template_ids"][
        0
    ]  # one of the three items excluded: a full sync would remove 33 %
    excludes = [{"id": "x", "criteria": {"contains": {"title": "Орбіт"}}}]
    r = await c.patch(f"/api/v1/templates/{tid}", json={"excludes": excludes})
    assert r.status_code == 200, r.text
    manifest = flash / ".syncmusic" / "manifest.jsonl"
    lines = [json.loads(x) for x in manifest.read_text().splitlines()]
    stale = next(x for x in lines if x.get("meta_hash") and "Орбіт" not in x.get("path", ""))
    stale["meta_hash"] = "stale"  # the library's tags of a kept item changed
    manifest.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines))

    report = await _job(app, await c.post("/api/v1/devices/flash/update"))
    assert "skipped" not in report["message"], report["message"]
    assert report["counts"]["replace"] == 1 and report["counts"]["remove"] == 0
    assert _files(flash) == before


async def test_update_removals_above_the_threshold_are_held_back_with_a_warning(
    env, music, tmp_path
):
    """with update removals on, removing more than the delete threshold waits for a full sync; the rest of
    the update is still written and the report says what was held back."""
    app, c = env
    flash = tmp_path / "flash"
    await _register(c, flash, name="Flash")
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    before = _files(flash)
    cfg = (await c.get("/api/v1/devices/flash")).json()["config"]
    excludes = [{"id": "x", "criteria": {"contains": {"title": "Орбіт"}}}]
    r = await c.patch(
        f"/api/v1/templates/{cfg['rule_set']['template_ids'][0]}", json={"excludes": excludes}
    )
    assert r.status_code == 200, r.text
    r = await c.patch("/api/v1/devices/flash", json={"update_removals": True})
    assert r.status_code == 200, r.text

    report = await _job(app, await c.post("/api/v1/devices/flash/update"))
    assert report["status"] == "succeeded" and report["counts"]["remove"] == 0
    assert report["warnings"] == [
        "1 of 3 items to remove (more than 20 %) are left for a full sync to review"
    ]
    assert _files(flash) == before

    r = await c.patch("/api/v1/devices/flash", json={"delete_threshold_pct": 50})
    assert r.status_code == 200, r.text
    report = await _job(app, await c.post("/api/v1/devices/flash/update"))
    assert report["counts"]["remove"] == 1 and report["warnings"] == []
    assert len(_files(flash)) == len(before) - 1


async def test_a_user_action_waits_for_an_update_that_is_finishing(
    env, music, tmp_path, monkeypatch
):
    """a job's status is final before it lets go of the device lock (it stores its last progress first);
    an Apply in that moment must wait for it instead of answering 'busy'."""
    app, c = env
    jobs = app.jobs
    gate = asyncio.Event()

    async def heartbeat(self, info, stop):
        await stop.wait()
        await gate.wait()  # the last progress store is slow

    monkeypatch.setattr(type(jobs), "_heartbeat", heartbeat)

    async def update(ctx):
        return {}

    info = jobs.submit("update", "update Flash", update, device_id="flash")
    for _ in range(100):
        if info.status.value == "succeeded":
            break
        await asyncio.sleep(0)
    assert info.status.value == "succeeded" and jobs.device_busy("flash")

    waiting = asyncio.create_task(app.sync._yield_update("flash"))
    await asyncio.sleep(0)
    assert not waiting.done()  # it waits for the job's end, not for its status
    gate.set()
    await waiting
    assert not jobs.device_busy("flash")


async def test_full_sync_report_counts_the_new_file_as_add(env, music, tmp_path):
    """a plan with one add (and keeps) reported add 0 after applying it."""
    app, c = env
    flash = tmp_path / "flash"
    await _register(c, flash, name="Flash")
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    # same tags as an existing track: the new file's name gets ' (2)' (the case seen in the repro)
    make_music(
        music / "Тартак" / "Орбіт копія.mp3",
        title="Орбіт без цукру",
        artist="Тартак",
        album="Гранатовий альбом",
    )
    await _job(app, await c.post("/api/v1/libraries/music/refresh"))
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    assert plan["counts"]["add"] == 1 and plan["counts"]["keep"] == 3
    report = await _apply(app, c, plan["id"])
    assert report["counts"]["add"] == 1 and report["counts"]["keep"] == 3, report["counts"]


async def test_full_sync_report_counts_a_new_video_episode_as_add(env, tmp_path):
    """As seen: a flash drive with videos, a new episode with the same title as one already there."""
    app, c = env
    show = tmp_path / "vid" / "Мультики"
    make_video(show / "Season 1" / "Мультики S01E01 Пілот.mp4")
    make_video(show / "Season 1" / "Мультики S01E02 Друга.mp4")
    lib = {
        "id": "video",
        "type": "folder",
        "name": "Відео",
        "settings": {"root": str(tmp_path / "vid"), "default_kind": "video"},
    }
    assert (await c.post("/api/v1/libraries", json=lib)).status_code == 201
    await _job(app, await c.post("/api/v1/libraries/video/refresh"))
    flash = tmp_path / "flash"
    await _register(c, flash, name="Flash", library_ids=["video"])
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    make_video(show / "Season 2" / "Мультики S02E01 Пілот.mp4")
    await _job(app, await c.post("/api/v1/libraries/video/refresh"))
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    assert plan["counts"]["add"] == 1 and plan["counts"]["keep"] == 2, plan["counts"]
    report = await _apply(app, c, plan["id"])
    assert report["counts"]["add"] == 1 and report["counts"]["keep"] == 2, report["counts"]
    assert len(list(flash.rglob("*.mp4"))) == 3


async def test_device_plugged_in_at_start_up_gets_no_sync(env, tmp_path):
    """after a daemon (re)start the start-up scan only re-discovers a device that is plugged in -
    no on-connect sync, prompt or update sync; a real re-plug behaves as before."""
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    disk = BlockDeviceInfo(name="sdz", path="/dev/sdz")
    q = app.bus.subscribe()
    for on_connect in ("auto", "ask"):
        await c.patch("/api/v1/devices/flash", json={"on_connect": on_connect})
        jobs = len(app.jobs.jobs())
        await app.sync.on_disk_added(ConnectedDisk(disk=disk, device_id="flash"), startup=True)
        await _settle(app)
        assert len(app.jobs.jobs()) == jobs
        assert not any(q.get_nowait().type == "sync.needs_confirmation" for _ in range(q.qsize()))
    app.started = False  # the app's start-up scan runs before `started` is set
    await app._disk_added(disk)
    await _settle(app)
    assert len(app.jobs.jobs()) == jobs
    app.started = True
    await app.sync.on_disk_added(
        ConnectedDisk(disk=disk, device_id="flash")
    )  # re-plug, still "ask"
    await _settle(app)
    assert any(q.get_nowait().type == "sync.needs_confirmation" for _ in range(q.qsize()))


async def test_update_removes_an_item_deleted_from_the_library(env, music, tmp_path):
    """an update removes what its library deleted (under the delete threshold) and says so; with
    `remove_deleted` off it stays as an orphan."""
    app, c = env
    flash = tmp_path / "flash"
    await _register(c, flash, name="Flash")
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    before = _files(flash)
    (music / "Скрябін" / "02 Зачини за мною двері.mp3").unlink()
    await _job(app, await c.post("/api/v1/libraries/music/refresh"))
    r = await c.patch(
        "/api/v1/devices/flash", json={"delete_threshold_pct": 50, "remove_deleted": False}
    )
    assert r.status_code == 200, r.text
    report = await _job(app, await c.post("/api/v1/devices/flash/update"))
    assert report["counts"]["remove"] == 0 and _files(flash) == before

    r = await c.patch("/api/v1/devices/flash", json={"remove_deleted": True})
    assert r.status_code == 200, r.text
    report = await _job(app, await c.post("/api/v1/devices/flash/update"))
    assert report["counts"]["remove"] == 1, report
    assert "removed 1 deleted from the library" in report["message"]
    assert len(_files(flash)) == len(before) - 1


async def test_connect_without_medium_waits_for_it(env, tmp_path, monkeypatch):
    """a USB disk without a medium / file system yet (a HiBy that has not shared its storage) is
    'connected, storage not shared': no on-connect update then (it would fail 'not connected'), a clear error on
    Sync, and the on-connect update runs once the medium appears (the watcher reports the disk again)."""
    app, c = env
    cfg = await _register(c, tmp_path / "flash", name="Flash")
    assert cfg["update_sync"]
    stored = await app.store.device("flash")
    await app.store.put_device(stored.model_copy(update={"mount_path": None}))
    await app.sync.load()
    spawned: list[str] = []
    monkeypatch.setattr(app.sync, "_spawn_update", lambda device_id, why, **kw: spawned.append(why))
    disk = BlockDeviceInfo(name="sdz", path="/dev/sdz", tran="usb", size=0)
    cd = ConnectedDisk(disk=disk, device_id="flash")
    app.sync.detector.connected["sdz"] = cd
    await app.sync.on_disk_added(cd)
    assert spawned == []
    dev = next(
        d for d in (await c.get("/api/v1/devices")).json()["registered"] if d["id"] == "flash"
    )
    assert dev["connected"] and dev["no_medium"]
    with pytest.raises(SyncError) as e:
        await app.sync.open_device("flash")
    assert e.value.code == "no_medium" and "USB storage" in str(e.value)

    cd.data_partition = "/dev/sdz1"  # the medium appeared: the watcher reports the disk again
    await app.sync.on_disk_added(cd)
    assert spawned == ["on connect"]
    dev = next(
        d for d in (await c.get("/api/v1/devices")).json()["registered"] if d["id"] == "flash"
    )
    assert not dev["no_medium"]


@pytest.mark.parametrize(
    ("choice", "update", "status", "ejected"),
    [
        ("off", False, "succeeded", False),
        ("full", False, "succeeded", True),
        ("full", True, "succeeded", False),
        ("any", True, "succeeded", True),
        ("any", False, "partial", False),
        ("any", False, "failed", False),
    ],
)
async def test_eject_after_a_sync(env, tmp_path, monkeypatch, choice, update, status, ejected):
    """`sync.eject_after` off / full / any ejects once the sync's job let go of the device; a sync
    that did not succeed is not ejected and says so."""
    from syncmusic.core.models import SyncReport

    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    cfg = await app.store.device("flash")
    cfg = cfg.model_copy(
        update={"mount_path": None, "sync": cfg.sync.model_copy(update={"eject_after": choice})}
    )
    calls: list[str] = []

    async def eject(device_id: str) -> None:
        assert not app.jobs.device_busy(device_id)  # after the job, never while it holds the device
        calls.append(device_id)

    monkeypatch.setattr(app.sync, "eject", eject)
    report = SyncReport(id="r", device_id="flash", status=status)

    async def job(ctx):
        app.sync._plan_auto_eject(cfg, SimpleNamespace(network=False), report, ctx.info.id, update)
        await asyncio.sleep(0.05)
        return {}

    info = app.jobs.submit("sync", "sync Flash", job, device_id="flash")
    await app.jobs.wait(info.id)
    await asyncio.gather(*app.sync._update_tasks)
    assert calls == (["flash"] if ejected else [])
    assert report.auto_eject is ejected
    if choice != "off" and status != "succeeded":
        assert any("not ejected" in w for w in report.warnings)


async def test_update_that_sent_data_publishes_update_sent(env, tmp_path, monkeypatch):
    """an update whose reverse sync sent listening data publishes `update.sent` with the counts;
    the notifications centre links it to the update's report. Nothing sent: no event."""
    from syncmusic.infra.notify import inbox_entry

    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    q = app.bus.subscribe()

    async def run(cfg, device, libraries, unavailable=None, playlist_choices=None):
        return ReverseReport(plays_submitted=3, stars_pushed=1)

    monkeypatch.setattr(app.sync.reverse_sync, "run", run)
    await _job(app, await c.post("/api/v1/devices/flash/update"))
    events = [q.get_nowait() for _ in range(q.qsize())]
    sent = [e for e in events if e.type == "update.sent"]
    assert len(sent) == 1 and sent[0].data["plays"] == 3 and sent[0].data["hearts"] == 1
    level, link = inbox_entry(sent[0])
    assert level == "info" and link == f"/devices/flash/sync/{sent[0].data['job_id']}"

    async def nothing(cfg, device, libraries, unavailable=None, playlist_choices=None):
        return ReverseReport()

    monkeypatch.setattr(app.sync.reverse_sync, "run", nothing)
    await _job(app, await c.post("/api/v1/devices/flash/update"))
    assert not any(q.get_nowait().type == "update.sent" for _ in range(q.qsize()))

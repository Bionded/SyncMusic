"""Settings → System → Storage & resources: every part with its size, each clear action frees exactly its part and
is refused while a job runs where a job may use it; the process's memory and CPU."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import asyncio

from syncmusic.devices import _filedevice
from tests.unit.test_api_sync import env, music  # noqa: F401


def _write(path, n: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * n)


def _parts(body):
    return {p["id"]: p for p in body["parts"]}


async def _seed(app):
    cache, data = app.config.cache_dir, app.config.data_dir
    _write(cache / "device-backups" / "pod" / "iTunesDB", 700)
    _write(cache / "device-backups" / "hiby" / "db" / "library.db", 300)
    _write(cache / "device-tags" / "stick.json", 120)
    _write(cache / "library-mirror" / "nas" / "a.mp3", 400)
    _write(cache / "work" / "leftover.part", 50)
    _write(cache / "thumbs" / "x.jpg", 30)
    _write(app.config.logs / "syncmusic.log.1", 90)
    _write(app.config.logs / "syncmusic.log.2", 10)
    return cache, data


async def test_report_lists_every_part_with_its_size(env):
    app, c = env
    await _seed(app)
    r = await c.get("/api/v1/system/storage")
    assert r.status_code == 200, r.text
    parts = _parts(r.json())
    assert {
        "database",
        "db_backups",
        "logs",
        "snapshots",
        "media_cache",
        "system_cache",
        "device_backups",
        "device_tags",
        "library_mirrors",
        "temp",
    } <= set(parts)
    assert parts["database"]["size"] > 0 and parts["database"]["action"] == "Compact"
    assert parts["device_backups"]["size"] == 1000
    assert [i["id"] for i in parts["device_backups"]["items"]] == ["pod", "hiby"]
    assert parts["device_tags"]["items"] == [{"id": "stick", "label": "stick", "size": 120}]
    assert parts["library_mirrors"]["size"] == 400 and parts["temp"]["size"] == 50
    assert parts["logs"]["size"] >= 100 and parts["system_cache"]["size"] == 30
    assert all(p["paths"] for p in parts.values())
    assert r.json()["filesystems"] and r.json()["filesystems"][0]["total"] > 0


async def test_each_clear_frees_its_part_only(env):
    app, c = env
    cache, _ = await _seed(app)
    r = await c.post("/api/v1/system/storage/device_backups/clear", json={"item": "pod"})
    assert r.status_code == 200, r.text
    assert r.json()["freed"] == 700
    assert (
        not (cache / "device-backups" / "pod").exists()
        and (cache / "device-backups" / "hiby").exists()
    )
    parts = _parts(r.json())
    assert parts["library_mirrors"]["size"] == 400 and parts["temp"]["size"] == 50
    r = await c.post("/api/v1/system/storage/logs/clear")
    assert r.json()["freed"] == 100 and not list(app.config.logs.glob("syncmusic.log.*"))
    for part, freed in (("device_tags", 120), ("library_mirrors", 400), ("temp", 50)):
        r = await c.post(f"/api/v1/system/storage/{part}/clear")
        assert r.status_code == 200 and r.json()["freed"] == freed, part
    assert (await c.post("/api/v1/system/storage/database/clear")).status_code == 200
    assert (await c.post("/api/v1/system/storage/runtime/clear")).status_code == 400
    assert (await c.post("/api/v1/system/storage/nope/clear")).status_code == 400
    r = await c.post("/api/v1/system/storage/device_backups/clear", json={"item": "ghost"})
    assert r.status_code == 404


async def test_refused_while_a_job_runs_except_safe_parts(env):
    app, c = env
    await _seed(app)
    blocker = asyncio.Event()

    async def job(ctx):
        await blocker.wait()

    running = app.jobs.submit("test", "hold", job)
    for part in ("device_backups", "temp", "database", "media_cache", "snapshots"):
        r = await c.post(f"/api/v1/system/storage/{part}/clear")
        assert r.status_code == 409, part
    assert (await c.post("/api/v1/system/storage/system_cache/clear")).status_code == 200
    assert (await c.post("/api/v1/system/resources/free")).status_code == 409
    blocker.set()
    await app.jobs.wait(running.id)


async def test_snapshots_are_listed_and_dropped(env):
    app, c = env
    lib = await app.sync.library("music")
    await app.sync.snapshots.get(lib)
    parts = _parts((await c.get("/api/v1/system/storage")).json())
    assert [i["id"] for i in parts["snapshots"]["items"]] == ["music"]
    assert parts["snapshots"]["size"] > 0
    r = await c.post("/api/v1/system/storage/snapshots/clear", json={"item": "music"})
    assert r.status_code == 200 and _parts(r.json())["snapshots"]["size"] == 0


async def test_resources_and_free_memory(env):
    app, c = env
    lib = await app.sync.library("music")
    await app.sync.snapshots.get(lib)
    _filedevice._TAG_CACHE["x"] = (1, 1, {}, None)
    from syncmusic.core import manifest as manifest_io
    from syncmusic.core.models import Manifest

    manifest_io._CACHE.clear()
    manifest_io._CACHE["/media/x"] = ((1, 1, b"", 0), Manifest.model_construct(items={}))
    r = await c.get("/api/v1/system/resources")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rss"] > 10 * 1024**2 and body["threads"] >= 1 and body["cores"] >= 1
    assert body["uptime_s"] >= 0 and body["loop_lag_ms"] >= 0 and body["cpu_percent"] >= 0
    caches = {x["id"]: x for x in body["caches"]}
    assert caches["snapshots"]["entries"] == 1 and caches["snapshots"]["items"] == 3
    assert caches["manifests"]["entries"] == 1
    r = await c.post("/api/v1/system/resources/free")
    assert r.status_code == 200, r.text
    caches = {x["id"]: x for x in r.json()["caches"]}
    assert caches["snapshots"]["entries"] == 0 and caches["tags"]["entries"] == 0
    assert caches["manifests"]["entries"] == 0 and not manifest_io._CACHE
    # dropped from memory only: the next use loads it from the database again
    assert len((await app.sync.snapshots.get(lib)).tracks) == 3


def test_trim_memory_runs_here():
    from syncmusic.infra import storage

    assert storage.trim_memory() is True  # glibc on the test hosts


async def test_free_memory_and_finished_jobs_trim(env, monkeypatch):
    from syncmusic.infra import storage

    app, c = env
    calls = []
    monkeypatch.setattr(storage, "trim_memory", lambda: calls.append(1) or True)
    assert (await c.post("/api/v1/system/resources/free")).status_code == 200
    assert calls == [1]

    async def job(ctx):
        return None

    await app.jobs.wait(app.jobs.submit("test", "x", job).id)
    for _ in range(50):
        if len(calls) == 2:
            break
        await asyncio.sleep(0.02)
    assert len(calls) == 2


def test_freeze_gc_and_collect_frozen():
    import gc

    from syncmusic.infra import storage

    keep = [object() for _ in range(100)]
    try:
        storage.freeze_gc()
        first = gc.get_freeze_count()
        assert first >= len(keep)
        storage.collect_frozen()  # unfreezes first: the count does not pile up
        assert gc.get_freeze_count() < 1.5 * first
    finally:
        gc.unfreeze()


async def test_finished_job_freezes_gc_when_idle(env, monkeypatch):
    from syncmusic.infra import storage

    app, _c = env
    calls = []
    monkeypatch.setattr(storage, "freeze_gc", lambda: calls.append(1))

    async def job(ctx):
        return None

    await app.jobs.wait(app.jobs.submit("test", "x", job).id)
    for _ in range(50):
        if calls:
            break
        await asyncio.sleep(0.02)
    assert calls == [1]


async def test_free_memory_drops_only_keep_plans(env):
    from syncmusic.core.models import ActionKind, ItemRef, PlanAction, SyncPlan
    from syncmusic.core.sync import PlanContext

    app, c = env
    ref = ItemRef(library_id="music", item_id="1")
    keeps = SyncPlan(id="k", device_id="d1", actions=[PlanAction(kind=ActionKind.KEEP, ref=ref)])
    adds = SyncPlan(id="a", device_id="d2", actions=[PlanAction(kind=ActionKind.ADD, ref=ref)])
    touched = SyncPlan(
        id="t", device_id="d3", actions=[PlanAction(kind=ActionKind.KEEP, ref=ref, restamp=True)]
    )
    for p in (keeps, adds, touched):
        app.sync.plans[p.id] = PlanContext(p, [])
    assert (await c.post("/api/v1/system/resources/free")).status_code == 200
    assert set(app.sync.plans) == {"a", "t"}
    assert (await c.get("/api/v1/plans/k")).status_code == 404

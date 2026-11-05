"""Settings → Cache & space: cache size and Clear cache, history and log retention, the low-disk
notification."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta

from syncmusic.core.models import SyncReport
from syncmusic.infra import housekeeping, notify
from syncmusic.infra.events import Event
from syncmusic.infra.jobs import JobInfo, JobStatus
from tests.unit.test_api_sync import env, music  # noqa: F401


def test_only_old_rotated_logs_are_deleted(tmp_path):
    now = time.time()
    for name, age_days in (
        ("syncmusic.log", 40),
        ("syncmusic.log.1", 2),
        ("syncmusic.log.2", 20),
        ("other", 40),
    ):
        f = tmp_path / name
        f.write_text("x")
        os.utime(f, (now - age_days * 86400, now - age_days * 86400))
    assert housekeeping.prune_logs(tmp_path, 14, now) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "other",
        "syncmusic.log",
        "syncmusic.log.1",
    ]
    assert housekeeping.prune_logs(tmp_path / "missing", 14) == 0


def test_low_disk_is_said_once_per_crossing():
    w = housekeeping.LowDisk()
    assert w.crossed(10, 5) is False
    assert w.crossed(4, 5) is True
    assert w.crossed(3, 5) is False  # still low: not again
    assert w.crossed(6, 5) is False
    assert w.crossed(4, 5) is True
    assert w.crossed(1, None) is False  # off


async def test_history_older_than_the_setting_is_pruned_but_the_last_report_stays(env):
    app, _c = env
    old = datetime.now(UTC) - timedelta(days=100)
    for i, (dev, kind, at) in enumerate(
        [
            ("a", "full", old),
            ("a", "full", old - timedelta(days=1)),
            ("a", "update", old),
            ("b", "full", datetime.now(UTC)),
            ("b", "full", old),
        ]
    ):
        await app.store.put_report(SyncReport(id=f"r{i}", device_id=dev, kind=kind, started_at=at))
    job = JobInfo(
        id="j-old",
        kind="sync",
        name="old",
        status=JobStatus.SUCCEEDED,
        created_at=old,
        finished_at=old,
    )
    await app.store.put_job(job)
    deleted = await app.store.prune_history(datetime.now(UTC) - timedelta(days=90))
    left = {r.id for r in await app.store.reports()}
    # a's newest full and update stay; a's older full and b's old one go; the old job goes
    assert left == {"r0", "r2", "r3"} and deleted == 3
    assert "j-old" not in {j.id for j in await app.store.recent_jobs()}


async def test_cache_info_and_clear(env, tmp_path):
    app, c = env
    src = tmp_path / "f.bin"
    src.write_bytes(b"x" * 1000)
    await app.cache.put("k1", {"f.bin": src})
    info = (await c.get("/api/v1/settings/cache")).json()
    assert info["used"] == 1000 and info["max"] == app.cache.max_bytes
    assert info["disk_total"] >= info["disk_free"] > 0
    assert info["pinned"] == 0
    async with app.cache.pinned("k1"):  # held by a sync
        assert (await c.get("/api/v1/settings/cache")).json()["pinned"] == 1000

    blocker = asyncio.Event()

    async def job(ctx):
        await blocker.wait()

    running = app.jobs.submit("test", "hold", job)
    r = await c.post("/api/v1/settings/cache/clear")
    assert r.status_code == 409 and "job is running" in r.json()["message"]
    blocker.set()
    await app.jobs.wait(running.id)
    r = await c.post("/api/v1/settings/cache/clear")
    assert r.status_code == 200, r.text
    assert r.json()["freed"] == 1000 and r.json()["used"] == 0


async def test_low_disk_notification(env, monkeypatch):
    app, c = env
    await c.patch("/api/v1/settings", json={"disk_warn_gb": 50})
    monkeypatch.setattr("syncmusic.app.disk_free", lambda _p: (100 * 1024**3, 2 * 1024**3))
    await app.check_disk()
    await app.check_disk()  # once per crossing
    for _ in range(100):
        items = (await c.get("/api/v1/notifications")).json()["items"]
        if items:
            break
        await asyncio.sleep(0.02)
    low = [i for i in items if i["event"] == "storage.low"]
    assert len(low) == 1 and low[0]["level"] == "warning" and low[0]["link"] == "/settings"
    s = notify.summary(Event(type="storage.low", data={"free": 9.6e9, "limit": 10 * 1000**3}), None)
    assert s == "SyncMusic's disk has 9.6 GB free (warning below 10 GB)"


async def test_system_cache_size_and_clear(env):
    """Settings shows the system cache (thumbnails, previews) with its size; Clear empties it at any
    time and leaves the media cache alone; a hard-linked shared thumbnail counts once."""
    app, c = env
    root = app.config.cache_dir
    (root / "thumbs" / "shared").mkdir(parents=True)
    (root / "thumbs" / "shared" / "a.jpg").write_bytes(b"x" * 300)
    (root / "thumbs" / "dev").mkdir()
    os.link(root / "thumbs" / "shared" / "a.jpg", root / "thumbs" / "dev" / "b.jpg")
    (root / "preview").mkdir()
    (root / "preview" / "p.mp3").write_bytes(b"y" * 200)
    info = (await c.get("/api/v1/settings/cache")).json()
    assert info["system"] == 500
    r = await c.post("/api/v1/settings/cache/clear", json={"kind": "system"})
    assert r.status_code == 200, r.text
    assert r.json()["freed"] == 500 and r.json()["system"] == 0
    assert not (root / "thumbs").exists()
    assert (await c.post("/api/v1/settings/cache/clear", json={"kind": "x"})).status_code == 400


async def test_notifications_expire_after_the_setting(env):
    """housekeeping deletes notifications (read or not) older than notification_days; never = kept."""
    app, c = env
    for ev in ("old-read", "old-unread", "edge", "new"):
        await app.store.add_notification(ev, "info", {})
    now = datetime.now(UTC)
    ages = {"old-read": 8, "old-unread": 30, "edge": 6.99, "new": 0}
    for ev, days in ages.items():
        await app.store.db.execute(
            "UPDATE notifications SET ts = ?, read = ? WHERE event = ?",
            ((now - timedelta(days=days)).isoformat(), int(ev == "old-read"), ev),
        )
    assert (await c.get("/api/v1/settings")).json()["notification_days"] == 7
    await c.patch("/api/v1/settings", json={"notification_days": None})
    await app.housekeeping()
    assert len(await app.store.notifications()) == 4  # never: all kept
    await c.patch("/api/v1/settings", json={"notification_days": 7})
    q = app.bus.subscribe()
    await app.housekeeping()
    seen: list[Event] = [q.get_nowait() for _ in range(q.qsize())]
    assert {n["event"] for n in await app.store.notifications()} == {"edge", "new"}
    assert any(e.type == "notification.cleared" for e in seen)

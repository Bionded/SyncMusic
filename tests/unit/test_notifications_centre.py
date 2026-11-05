"""The in-app notifications centre: events stored with a level and a UI link, unread count,
mark read, clear, the newest kept, persisted across a restart."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import asyncio

from syncmusic.app import Application
from syncmusic.core.sync import SyncService
from syncmusic.infra import notify, store
from syncmusic.infra.events import Event
from tests.unit.test_api_sync import _register, env, music  # noqa: F401


async def _items(c, event=None, n=1):
    for _ in range(200):
        r = (await c.get("/api/v1/notifications")).json()
        items = [i for i in r["items"] if event is None or i["event"] == event]
        if len(items) >= n:
            return r, items
        await asyncio.sleep(0.02)
    raise AssertionError(f"no {event} notification: {r}")


async def _sync(app, c, tmp_path):
    await _register(c, tmp_path / "flash", name="Flash")
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    r = await c.post(f"/api/v1/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert r.status_code == 202, r.text
    return r.json()["id"], await app.jobs.wait(r.json()["id"])


async def test_sync_finished_is_stored_with_a_link_and_announced(env, tmp_path):
    app, c = env
    q = app.bus.subscribe()
    job_id, job = await _sync(app, c, tmp_path)
    assert job.status == "succeeded"
    r, (n,) = await _items(c, "sync.finished")
    assert n["level"] == "info" and n["read"] is False
    assert n["device_id"] == "flash" and n["device_name"] == "Flash"
    assert n["link"] == f"/devices/flash/sync/{job_id}"
    assert n["data"]["status"] == "succeeded" and n["data"]["failed"] == 0
    assert r["unread"] >= 1

    # the row is stored a moment before the event is published: wait for it
    async def announced() -> None:
        while True:
            e = await q.get()
            if e.type == "notification.created" and e.data["id"] == n["id"]:
                return

    await asyncio.wait_for(announced(), 5)
    # the webhook settings do not matter: nothing is configured and it is still stored
    assert app.notifications.notifiers == []


async def test_a_failing_sync_is_an_error_notification(env, tmp_path, monkeypatch):
    app, c = env

    async def broken(self, *a, **kw):
        raise RuntimeError("the device did not open")

    monkeypatch.setattr(SyncService, "_run_apply", broken)
    job_id, job = await _sync(app, c, tmp_path)
    assert job.status == "failed"
    _, (n,) = await _items(c, "sync.failed")
    assert n["level"] == "error" and n["data"]["error"] == "the device did not open"
    assert n["link"] == f"/devices/flash/sync/{job_id}"
    assert not [
        i
        for i in (await c.get("/api/v1/notifications")).json()["items"]
        if i["event"] == "sync.finished"
    ]


async def test_mark_read_clear_and_the_newest_kept(env, monkeypatch):
    app, c = env
    monkeypatch.setattr(store, "NOTIFICATIONS_LIMIT", 3)
    await c.delete("/api/v1/notifications")
    for i in range(5):
        await app.store.add_notification("device.connected", "info", {"i": i}, device_id="d")
    r = (await c.get("/api/v1/notifications")).json()
    assert [n["data"]["i"] for n in r["items"]] == [4, 3, 2] and r["unread"] == 3
    first = r["items"][0]["id"]
    assert (await c.post("/api/v1/notifications/read", json={"ids": [first]})).json() == {
        "unread": 2
    }
    r = (await c.get("/api/v1/notifications")).json()
    assert [n["read"] for n in r["items"]] == [True, False, False]
    assert (await c.post("/api/v1/notifications/read", json={})).json() == {"unread": 0}
    assert (await c.delete("/api/v1/notifications")).json() == {"unread": 0}
    assert (await c.get("/api/v1/notifications")).json() == {
        "items": [],
        "unread": 0,
        "keep_days": 7,
    }


async def test_clear_one_clear_all_and_expiry(env):
    """Clear deletes one notification (read or not), Clear all every one; old ones are dropped by
    housekeeping (604); opening still only marks read."""
    app, c = env
    await c.delete("/api/v1/notifications")
    old = await app.store.add_notification("device.connected", "info", {"i": 0}, device_id="d")
    await app.store.db.execute(
        "UPDATE notifications SET ts = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", old["id"])
    )
    ids = [
        (await app.store.add_notification("device.connected", "info", {"i": i}, device_id="d"))[
            "id"
        ]
        for i in (1, 2)
    ]
    await app.housekeeping()
    r = (await c.get("/api/v1/notifications")).json()
    assert [n["data"]["i"] for n in r["items"]] == [2, 1]  # the 2000 one is gone
    assert (await c.post("/api/v1/notifications/read", json={"ids": [ids[0]]})).json() == {
        "unread": 1
    }
    assert (await c.delete(f"/api/v1/notifications/{ids[1]}")).json() == {"unread": 0}
    r = (await c.get("/api/v1/notifications")).json()
    # the read one stays until it is cleared
    assert [n["id"] for n in r["items"]] == [ids[0]] and r["unread"] == 0
    await app.store.add_notification("device.connected", "info", {"i": 3}, device_id="d")
    assert (await c.delete("/api/v1/notifications")).json() == {"unread": 0}
    assert (await c.get("/api/v1/notifications")).json()["items"] == []
    assert (await c.post("/api/v1/notifications/unread", json={})).status_code in (404, 405)


async def test_kept_across_a_restart(config, music):  # noqa: F811
    app = Application(config)
    await app.start()
    await app.store.add_notification(
        "device.unknown", "info", {"model": "Stick"}, link="/devices/register"
    )
    await app.stop()
    app = Application(config)
    await app.start()
    try:
        (n,) = await app.store.notifications()
        assert n["event"] == "device.unknown" and n["link"] == "/devices/register" and not n["read"]
        assert await app.store.unread_notifications() == 1
    finally:
        await app.stop()


def test_levels_and_links():
    def entry(type_, **data):
        return notify.inbox_entry(Event(type=type_, data=data))

    assert entry("sync.started", device_id="f") is None
    assert entry("device.connected", device_id="my ipod") == ("info", "/devices/my%20ipod")
    assert entry("device.unknown", disk="/dev/sdx") == ("info", "/devices/register")
    assert entry("sync.needs_confirmation", device_id="f", plan_id="p1") == (
        "warning",
        "/devices/f/plan/p1",
    )
    assert entry("sync.needs_confirmation", device_id="f") == ("warning", "/devices/f")
    assert entry("sync.db_size_warning", device_id="f", level="near") == ("warning", "/devices/f")
    assert entry("sync.db_size_warning", device_id="f", level="over") == ("error", "/devices/f")
    assert entry("sync.finished", device_id="f", job_id="j", status="partial") == (
        "warning",
        "/devices/f/sync/j",
    )
    assert entry("sync.finished", device_id="f", job_id="j", status="failed")[0] == "error"
    assert entry("device.error", device_id="f", code="unmountable") == ("error", "/devices/f")
    assert entry("update.failed", device_id="f", job_id="j") == ("error", "/devices/f/sync/j")
    # a reverse sync only when it went wrong
    assert entry("reverse.finished", device_id="f", errors=[], failed=False) is None
    assert entry("reverse.finished", device_id="f", errors=["x"]) == ("error", "/devices/f/history")


async def test_an_event_off_for_in_app_is_not_stored(env, tmp_path):
    """Settings → Notifications' In app column: a successful sync is not stored when "Full sync
    finished" is off, a connected device still is."""
    app, c = env
    events = [e for e in notify.INBOX_EVENTS if e != "sync.finished"]
    r = await c.patch("/api/v1/settings", json={"inbox_events": events})
    assert r.status_code == 200, r.text
    _job, job = await _sync(app, c, tmp_path)
    assert job.status == "succeeded"
    app.bus.publish("device.connected", device_id="flash")
    await _items(c, "device.connected")
    items = (await c.get("/api/v1/notifications")).json()["items"]
    assert [i for i in items if i["event"] == "sync.finished"] == []
    bad = await c.patch("/api/v1/settings", json={"inbox_events": ["sync.started"]})
    assert bad.status_code == 400 and "not an in-app event" in bad.json()["message"]

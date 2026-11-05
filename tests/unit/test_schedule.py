"""Daily scheduled sync, with a fake clock: once a day within the window after `sync_at`, only when the
device is connected and idle; the setting is validated and offered in the device form."""
# ruff: noqa: F811 - pytest fixtures imported from test_api_sync

from __future__ import annotations

import datetime as dt
import shutil

import pytest
from pydantic import ValidationError

from syncmusic.core.models import DeviceConfig
from tests.media import make_music
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401

TZ = dt.timezone(dt.timedelta(hours=1))


def _at(day: int, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(2026, 10, day, hh, mm, tzinfo=TZ)


async def _synced(app, c, tmp_path, **kw):
    await _register(c, tmp_path / "flash", **kw)
    pc = await app.sync.plan("my-flash")
    await _apply(app, c, pc.plan.id, first_sync="keep")
    r = await c.patch("/api/v1/devices/my-flash", json={"sync_at": "03:00"})
    assert r.status_code == 200, r.text


async def _wait_jobs(app):
    for j in app.jobs.jobs():
        await app.jobs.wait(j.id)


async def test_daily_sync_runs_once_a_day_in_its_window(env, tmp_path, music):
    app, c = env
    await _synced(app, c, tmp_path)
    q = app.bus.subscribe()
    n = len(app.jobs.jobs())
    assert await app.sync.scheduled_syncs(_at(23, 2, 59)) == []
    make_music(
        music / "Скрябін" / "03 Нова.mp3", title="Нова", artist="Скрябін", album="Група крові"
    )
    assert await app.sync.scheduled_syncs(_at(23, 3, 0)) == ["my-flash"]
    await _wait_jobs(app)
    assert len(app.jobs.jobs()) == n + 1 and app.jobs.jobs()[0].status == "succeeded"
    assert len(list((tmp_path / "flash").rglob("*.mp3"))) == 4  # the new song arrived
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert any(e.type == "sync.scheduled" and e.data["at"] == "03:00" for e in events)
    # once a day
    assert await app.sync.scheduled_syncs(_at(23, 3, 30)) == []
    # the next day, inside the window; a day it is checked only after the window: no sync
    assert await app.sync.scheduled_syncs(_at(24, 3, 59)) == ["my-flash"]
    await _wait_jobs(app)
    assert await app.sync.scheduled_syncs(_at(25, 4, 0)) == []
    assert await app.sync.scheduled_syncs(_at(25, 2, 0)) == []


async def test_only_when_connected_and_idle(env, tmp_path):
    app, c = env
    await _synced(app, c, tmp_path)
    shutil.move(tmp_path / "flash", tmp_path / "away")  # unplugged at 03:00
    assert await app.sync.scheduled_syncs(_at(23, 3, 0)) == []
    shutil.move(tmp_path / "away", tmp_path / "flash")  # plugged in at 03:20: still in the window
    app.jobs.device_busy = lambda device_id: True  # a job runs on it
    assert await app.sync.scheduled_syncs(_at(23, 3, 20)) == []
    del app.jobs.device_busy
    assert await app.sync.scheduled_syncs(_at(23, 3, 21)) == ["my-flash"]
    await _wait_jobs(app)


async def test_a_plan_waiting_for_the_user_is_not_replaced(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", on_connect="ask")
    r = await c.patch("/api/v1/devices/my-flash", json={"sync_at": "03:00"})
    assert r.status_code == 200
    # never synced: the scheduled sync makes the first-sync plan and waits for the user
    assert await app.sync.scheduled_syncs(_at(23, 3, 0)) == ["my-flash"]
    w = app.sync._waiting["my-flash"]
    assert "first sync" in w["reason"] and w["plan_id"]
    assert app.jobs.jobs() == []
    assert await app.sync.scheduled_syncs(_at(24, 3, 0)) == []  # still waiting: left alone


def test_sync_at_is_validated_and_in_the_form(env):
    for bad in ("3:00", "24:00", "03:60", "noon"):
        with pytest.raises(ValidationError):
            DeviceConfig(id="d", type="flashdrive", name="D", sync_at=bad)
    assert DeviceConfig(id="d", type="flashdrive", name="D", sync_at="23:59").sync_at == "23:59"


async def test_general_schema_offers_the_time(env):
    app, c = env
    types = (await c.get("/api/v1/device-types")).json()
    general = next(t for t in types if t["type"] == "flashdrive")["general_schema"]["properties"]
    assert "sync_at" in general and general["sync_at"]["anyOf"][0]["format"] == "time"


# ---- sync windows


async def _windows(c, *spans: tuple[str, str]):
    body = {"sync_windows": [{"start": a, "end": b} for a, b in spans]}
    r = await c.patch("/api/v1/devices/my-flash", json=body)
    assert r.status_code == 200, r.text


async def test_daily_sync_needs_its_window(env, tmp_path):
    app, c = env
    await _synced(app, c, tmp_path)
    await _windows(c, ("05:00", "06:00"))
    assert await app.sync.scheduled_syncs(_at(23, 3, 0)) == []
    await _windows(c, ("23:00", "04:00"))  # over midnight
    assert await app.sync.scheduled_syncs(_at(23, 3, 0)) == ["my-flash"]
    await _wait_jobs(app)


async def test_on_connect_sync_waits_for_the_window(env, tmp_path):
    app, c = env
    await _synced(app, c, tmp_path)
    await _windows(c, ("02:00", "04:00"))
    app.sync._window_pending["my-flash"] = (
        "on connect"  # what an on-connect auto sync at 13:00 leaves
    )
    assert await app.sync.scheduled_syncs(_at(23, 13, 0)) == []
    assert await app.sync.scheduled_syncs(_at(24, 2, 0)) == ["my-flash"]
    await _wait_jobs(app)
    assert "my-flash" not in app.sync._window_pending
    # unplugged before the window: dropped
    app.sync._window_pending["my-flash"] = "on connect"
    shutil.move(tmp_path / "flash", tmp_path / "away")
    assert await app.sync.scheduled_syncs(_at(25, 2, 30)) == []
    assert "my-flash" not in app.sync._window_pending


async def test_update_checks_wait_for_the_window(env, tmp_path):
    app, c = env
    await _synced(app, c, tmp_path)
    await _windows(c, ("02:00", "04:00"))
    app.sync._update_dirty.add("my-flash")
    assert await app.sync.periodic_updates(now=10_000.0, clock=_at(23, 13, 0)) == []
    assert "my-flash" in app.sync._update_dirty  # the change is kept
    assert await app.sync.periodic_updates(now=10_001.0, clock=_at(24, 2, 10)) == ["my-flash"]
    await _wait_jobs(app)


async def test_status_says_when_the_window_opens(env, tmp_path):
    app, c = env
    await _synced(app, c, tmp_path)
    soon = dt.datetime.now().astimezone() + dt.timedelta(hours=2)
    start, end = soon.strftime("%H:%M"), (soon + dt.timedelta(hours=1)).strftime("%H:%M")
    await _windows(c, (start, end))
    dev = next(
        d for d in (await c.get("/api/v1/devices")).json()["registered"] if d["id"] == "my-flash"
    )
    assert dev["window_wait"] and dev["window_wait"][11:16] == start
    await _windows(c)  # no windows: any time
    dev = next(
        d for d in (await c.get("/api/v1/devices")).json()["registered"] if d["id"] == "my-flash"
    )
    assert dev["window_wait"] is None


async def test_a_waiting_update_holds_the_daily_sync_back(env, tmp_path, music):
    app, c = env
    await _synced(app, c, tmp_path)
    make_music(
        music / "Скрябін" / "03 Нова.mp3", title="Нова", artist="Скрябін", album="Група крові"
    )
    app.update_pending = True  # Update now after the running syncs
    assert app.sync.hold_automatic
    assert await app.sync.scheduled_syncs(_at(23, 3, 0)) == []
    assert await app.sync.periodic_updates() == []
    app.update_pending = False
    assert await app.sync.scheduled_syncs(_at(23, 3, 1)) == ["my-flash"]
    await _wait_jobs(app)

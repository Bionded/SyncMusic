"""iTunesDB size limit: the plan carries the current and estimated database size, warns near the 63 MiB
limit, needs its own confirmation over it, auto sync stops at plan review, the API and notifications expose it."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from types import SimpleNamespace

import pytest

from syncmusic.core.models import DeviceConfig, SyncPlan
from syncmusic.core.space import DB_WARN_FRACTION, check_db_size
from syncmusic.devices.iPod.ipod import ITUNESDB, ITUNESDB_LIMIT, IPod
from syncmusic.infra.events import Event
from syncmusic.infra.notify import summary
from tests.contracts.harness import ipod_tree
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401 - fixtures

MiB = 1024 * 1024


def test_limit_is_63_mib():
    assert IPod.db_size_limit == ITUNESDB_LIMIT
    assert ITUNESDB_LIMIT // MiB == 63 and ITUNESDB_LIMIT % MiB == 0


@pytest.mark.parametrize(
    ("estimated", "level"),
    [
        (10 * MiB, "ok"),
        (int(DB_WARN_FRACTION * 63 * MiB) - 1, "ok"),
        (57 * MiB, "warn"),
        (63 * MiB, "warn"),
        (63 * MiB + 1, "over"),
    ],
)
def test_levels_with_synthetic_sizes(estimated, level):
    plan = SyncPlan(id="p", device_id="d")
    db = check_db_size(plan, 5 * MiB, estimated, 63 * MiB)
    assert db.level == level and plan.db == db and db.current == 5 * MiB
    assert plan.needs_confirmation is (level == "over")
    assert bool(plan.warnings) is (level != "ok")
    if level == "over":
        assert "may fail to read it" in plan.confirmation_reasons[0]
        assert any("transcoding does not make it smaller" in s for s in plan.space.suggestions)


async def test_ipod_size_and_estimate(tmp_path):
    mount = ipod_tree(tmp_path / "pod")
    dev = IPod(
        DeviceConfig(id="p", type="ipod", name="P"),
        mount,
        None,
    )
    assert await dev.db_size() == (mount / ITUNESDB).stat().st_size
    empty = await dev.estimate_db_size(0, 0)
    assert await dev.estimate_db_size(1000, 0) > empty + 1000 * 500
    assert await dev.estimate_db_size(1000, 500) > await dev.estimate_db_size(1000, 0)
    (mount / ITUNESDB).unlink()
    assert await dev.db_size() is None


async def _pod(c, tmp_path):
    mount = ipod_tree(tmp_path / "ipod")
    dev = await _register(c, mount, name="iPod", type="ipod")
    return dev, mount


async def test_plan_and_space_expose_the_size(env, tmp_path):
    app, c = env
    dev, mount = await _pod(c, tmp_path)
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    size = (mount / ITUNESDB).stat().st_size
    assert plan["db"]["current"] == size and plan["db"]["limit"] == ITUNESDB_LIMIT
    assert plan["db"]["level"] == "ok" and plan["db"]["estimated"] > size
    space = (await c.get(f"/api/v1/devices/{dev['id']}/space")).json()
    assert space["db"] == {"size": size, "limit": ITUNESDB_LIMIT, "level": "ok"}
    flash = await _register(c, tmp_path / "stick", name="Stick")
    assert (await c.get(f"/api/v1/devices/{flash['id']}/space")).json()["db"] is None
    flash_plan = (await c.post(f"/api/v1/devices/{flash['id']}/plan", json={})).json()
    assert flash_plan["db"] is None


async def test_over_the_limit_needs_its_own_confirmation_and_notifies(env, tmp_path, monkeypatch):
    app, c = env
    dev, mount = await _pod(c, tmp_path)
    monkeypatch.setattr(IPod, "db_size_limit", (mount / ITUNESDB).stat().st_size + 100)
    events = app.bus.subscribe(64)
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    assert plan["db"]["level"] == "over" and plan["needs_confirmation"]
    seen = []
    while not events.empty():
        seen.append(events.get_nowait())
    warn = [e for e in seen if e.type == "sync.db_size_warning"]
    assert warn and warn[0].data["level"] == "over" and warn[0].data["device_id"] == dev["id"]
    r = await c.post(
        f"/api/v1/plans/{plan['id']}/apply", json={"first_sync": "keep", "confirm": True}
    )
    assert r.status_code == 409, r.text
    assert "confirm_db_limit" in r.text
    await _apply(app, c, plan["id"], first_sync="keep", confirm=True, confirm_db_limit=True)


async def test_auto_sync_stops_at_plan_review_over_the_limit(env, tmp_path, monkeypatch):
    app, c = env
    dev, mount = await _pod(c, tmp_path)
    # a first sync always waits for the keep/adopt/wipe choice: sync once so the auto plan is an ordinary one
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    await c.patch(f"/api/v1/devices/{dev['id']}", json={"on_connect": "auto"})
    monkeypatch.setattr(IPod, "db_size_limit", 1000)
    before = len(app.jobs.jobs())
    await app.sync.on_disk_added(SimpleNamespace(device_id=dev["id"], data_partition="/dev/sdz1"))
    assert "may fail to read it" in app.sync._waiting[dev["id"]]["reason"]
    assert len(app.jobs.jobs()) == before  # nothing applied


def test_notification_text():
    ev = Event(
        type="sync.db_size_warning",
        data={"level": "warn", "estimated": 60 * MiB, "limit": 63 * MiB},
    )
    assert summary(ev, "Classic") == (
        "Classic: the device database would be 60.0 MiB after the sync, close to its limit of 63 MiB"
    )


async def test_an_update_check_does_not_repeat_the_warning(env, tmp_path, monkeypatch):
    """every automatic update check of an iPod near its limit must not add a notification."""
    app, c = env
    dev, mount = await _pod(c, tmp_path)
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    monkeypatch.setattr(IPod, "db_size_limit", (mount / ITUNESDB).stat().st_size + 100)
    events = app.bus.subscribe(256)
    r = await c.post(f"/api/v1/devices/{dev['id']}/update")
    assert r.status_code == 202, r.text
    await app.jobs.wait(r.json()["id"])
    seen = []
    while not events.empty():
        seen.append(events.get_nowait().type)
    assert "update.finished" in seen and "sync.db_size_warning" not in seen
    await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})  # a plan for review still warns
    seen = []
    while not events.empty():
        seen.append(events.get_nowait().type)
    assert "sync.db_size_warning" in seen

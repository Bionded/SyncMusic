# SPDX-License-Identifier: GPL-3.0-or-later
"""The device lease: another installation's fresh lease refuses a sync, a stale one is taken over, the holder's own
lease is released at the end."""

# ruff: noqa: F811 - pytest fixtures imported from test_api_sync
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from syncmusic.core import lease as device_lease
from syncmusic.core.models import utcnow
from tests.contracts.harness import rockbox_tree
from tests.unit.test_api_sync import env, music  # noqa: F401


def _lease_file(mount: Path) -> Path:
    return mount / ".syncmusic" / device_lease.FILE_NAME


def _other(minutes_ago: float) -> bytes:
    beat = utcnow() - dt.timedelta(minutes=minutes_ago)
    return (
        device_lease.Lease(
            instance_id="other-instance",
            name="Kitchen box",
            job_id="j",
            started_at=beat,
            heartbeat_at=beat,
        )
        .model_dump_json()
        .encode()
    )


def test_blocking_rules():
    now = utcnow()
    mine = device_lease.Lease(instance_id="me", started_at=now, heartbeat_at=now)
    fresh = mine.model_copy(update={"instance_id": "them"})
    stale = fresh.model_copy(update={"heartbeat_at": now - dt.timedelta(minutes=11)})
    assert not device_lease.blocking(None, "me")
    assert not device_lease.blocking(mine, "me")
    assert device_lease.blocking(fresh, "me")
    assert not device_lease.blocking(stale, "me")
    assert device_lease.parse(b"") is None and device_lease.parse(b"garbage") is None


async def _register(c, mount: Path) -> str:
    body = {"name": "Mini", "type": "rockbox", "mount_path": str(mount), "library_ids": ["music"]}
    return (await c.post("/api/v1/devices/register", json=body)).json()["id"]


async def _sync(app, c, dev: str):
    plan = (await c.post(f"/api/v1/devices/{dev}/plan", json={})).json()
    r = await c.post(f"/api/v1/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert r.status_code == 202, r.text
    return await app.jobs.wait(r.json()["id"])


async def test_fresh_lease_of_another_installation_refuses_the_sync(env, tmp_path):
    app, c = env
    mount = rockbox_tree(tmp_path / "mini")
    dev = await _register(c, mount)
    _lease_file(mount).parent.mkdir(exist_ok=True)
    _lease_file(mount).write_bytes(_other(minutes_ago=2))
    job = await _sync(app, c, dev)
    assert job.status == "failed" and "being synced by Kitchen box" in (job.error or "")
    assert "Kitchen box" in _lease_file(mount).read_text()  # theirs, untouched


async def test_stale_lease_is_taken_over_and_released(env, tmp_path, caplog):
    app, c = env
    mount = rockbox_tree(tmp_path / "mini")
    dev = await _register(c, mount)
    _lease_file(mount).parent.mkdir(exist_ok=True)
    _lease_file(mount).write_bytes(_other(minutes_ago=30))
    job = await _sync(app, c, dev)
    assert job.status == "succeeded", job.error
    assert "taking over a stale lease of Kitchen box" in caplog.text
    assert not _lease_file(mount).exists()  # released


async def test_lease_is_held_during_the_job(tmp_path, monkeypatch):
    seen = []

    class Dev:
        def __init__(self):
            self.files: dict[str, bytes] = {}

        async def read_state(self, name):
            return self.files.get(name)

        async def write_state(self, name, data):
            self.files[name] = data

        async def delete_state(self, name):
            self.files.pop(name, None)

    dev = Dev()
    async with device_lease.held(dev, "me", "Here", "job-1"):
        seen.append(device_lease.parse(dev.files[device_lease.FILE_NAME]))
    assert seen[0].instance_id == "me" and seen[0].job_id == "job-1"
    assert dev.files == {}
    dev.files[device_lease.FILE_NAME] = _other(minutes_ago=1)
    with pytest.raises(device_lease.LeaseHeld, match="Kitchen box"):
        async with device_lease.held(dev, "me", "Here", "job-2"):
            pass


async def test_device_list_names_another_installations_lease(env, tmp_path):
    from syncmusic.infra.blockdev.mount import Mounted

    app, c = env
    mount = rockbox_tree(tmp_path / "mini")
    dev = await _register(c, mount)
    _lease_file(mount).parent.mkdir(exist_ok=True)
    _lease_file(mount).write_bytes(_other(minutes_ago=1))
    app.sync._mounts[dev] = Mounted(mount, "/dev/sdz1", "vfat", owned=False)
    try:
        listed = (await app.sync.list_devices())["registered"][0]
        assert listed["lease"]["name"] == "Kitchen box"
        _lease_file(mount).write_bytes(_other(minutes_ago=30))  # stale: not shown
        assert (await app.sync.list_devices())["registered"][0]["lease"] is None
    finally:
        app.sync._mounts.pop(dev, None)

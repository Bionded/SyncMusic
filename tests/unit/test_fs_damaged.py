"""A damaged device file system: an EIO/EUCLEAN from a device read gives a JSON error instead of a
500, marks the device 'file system damaged' (notified once), and automatic syncs stop until it is cleared."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import errno

from syncmusic.core.manifest import ManifestError
from syncmusic.core.sync import fs_damage
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401


def _eio() -> OSError:
    return OSError(errno.EIO, "Input/output error")


def test_fs_damage_finds_the_io_error_in_the_cause_chain():
    try:
        try:
            raise _eio()
        except OSError as e:
            raise ManifestError("manifest unreadable") from e
    except ManifestError as wrapped:
        assert fs_damage(wrapped) is not None
    assert fs_damage(OSError(errno.EUCLEAN, "Structure needs cleaning")) is not None
    assert fs_damage(OSError(errno.ENOENT, "gone")) is None
    assert fs_damage(ValueError("x")) is None


async def _device(c, name="flash"):
    devices = (await c.get("/api/v1/devices")).json()["registered"]
    return next(d for d in devices if d["id"] == name)


async def test_an_io_error_in_a_request_is_json_and_marks_the_device(env, tmp_path, monkeypatch):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    events = []
    orig_publish = app.bus.publish
    monkeypatch.setattr(
        app.bus, "publish", lambda t, **kw: (events.append((t, kw)), orig_publish(t, **kw))[1]
    )

    async def broken(device_id):
        try:
            raise _eio()
        except OSError as e:
            raise ManifestError(f"{device_id}: manifest unreadable") from e

    monkeypatch.setattr(app.sync, "space", broken)
    for _ in range(2):
        r = await c.get("/api/v1/devices/flash/space")
        assert r.status_code == 409, r.text
        assert r.json()["error"] == "fs_damaged"
    # notified once, not on every request
    assert [kw["code"] for t, kw in events if t == "device.error"] == ["fs_damaged"]

    d = await _device(c)
    assert d["fs_damaged"] == "Input/output error"
    assert "file system damaged" in d["error"]

    # no automatic or manual update while damaged
    assert await app.sync.periodic_updates(now=10**9) == []
    r = await c.post("/api/v1/devices/flash/update")
    assert r.status_code == 409 and r.json()["error"] == "fs_damaged", r.text
    # a folder device cannot be repaired with fsck
    r = await c.post("/api/v1/devices/flash/repair")
    assert r.status_code == 400, r.text


async def test_a_raw_os_error_is_json_not_a_traceback(env, tmp_path, monkeypatch):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")

    async def broken(device_id):
        raise OSError(errno.EUCLEAN, "Structure needs cleaning")

    monkeypatch.setattr(app.sync, "space", broken)
    r = await c.get("/api/v1/devices/flash/space")
    assert r.status_code == 409 and r.json()["error"] == "fs_damaged", r.text

    async def other(device_id):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(app.sync, "space", other)
    r = await c.get("/api/v1/devices/flash/space")
    assert r.status_code == 500 and r.json()["error"] == "io_error", r.text


async def test_a_failed_device_job_marks_the_device(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")

    async def job(ctx):
        raise _eio()

    info = app.jobs.submit("sync", "sync Flash", job, device_id="flash")
    assert (await app.jobs.wait(info.id)).status == "failed"
    assert (await _device(c))["fs_damaged"]

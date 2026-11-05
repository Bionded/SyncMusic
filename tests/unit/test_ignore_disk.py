"""Ignore an unknown disk: left out of `unknown` (dashboard, Devices list), listed under
`ignored`, remembered by its USB serial across replugs and restarts, undone with `ignore: false`."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.core.detection import ConnectedDisk
from syncmusic.core.sync import disk_key
from syncmusic.facade import AppFacade
from syncmusic.infra.blockdev.identify import BlockDeviceInfo


def _disk(path="/dev/sdx", serial="SN-1"):
    return BlockDeviceInfo(
        name=path.rsplit("/", 1)[1],
        path=path,
        serial=serial,
        vendor="Apple",
        model="iPod",
        tran="usb",
    )


def test_disk_key_prefers_serial_then_uuid():
    assert disk_key(_disk()) == "serial:SN-1"
    assert disk_key(BlockDeviceInfo(name="sdx", path="/dev/sdx", uuid="AB-12")) == "uuid:AB-12"
    assert disk_key(BlockDeviceInfo(name="sdx", path="/dev/sdx")) == "path:/dev/sdx"


async def test_ignore_and_undo(config):
    app = Application(config)
    await app.start()
    api = create_api(AppFacade(app), manage_lifecycle=False)
    try:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
            app.sync.detector.connected["sdx"] = ConnectedDisk(disk=_disk())
            assert [u["disk"] for u in (await c.get("/api/v1/devices")).json()["unknown"]] == [
                "/dev/sdx"
            ]
            r = await c.post("/api/v1/devices/ignore", json={"disk": "/dev/sdx"})
            assert r.status_code == 200 and r.json()["unknown"] == []
            assert [u["disk"] for u in r.json()["ignored"]] == ["/dev/sdx"]
            # plugged in again under another path: still ignored (by its serial), also after a reload
            del app.sync.detector.connected["sdx"]
            app.sync.detector.connected["sdy"] = ConnectedDisk(disk=_disk("/dev/sdy"))
            await app.sync.load()
            assert (await c.get("/api/v1/devices")).json()["unknown"] == []
            r = await c.post("/api/v1/devices/ignore", json={"disk": "/dev/sdy", "ignore": False})
            assert [u["disk"] for u in r.json()["unknown"]] == ["/dev/sdy"] and r.json()[
                "ignored"
            ] == []
            assert await app.store.ignored_disks() == []
            r = await c.post("/api/v1/devices/ignore", json={"disk": "/dev/nope"})
            assert r.status_code == 404
            # not an app setting: the Settings API does not show it
            assert "ignored_disks" not in (await c.get("/api/v1/settings")).json()
    finally:
        await app.stop()

"""device detection never stops the app, and GET /devices/diagnostics says why a disk is (not) listed."""
# ruff: noqa: F811 - pytest fixtures imported from test_api_sync

from __future__ import annotations

import sys
import types

from syncmusic import facade as facade_mod
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.unit.test_api_sync import env, music  # noqa: F401
from tests.unit.test_detection import Scan, _watcher, disk


async def test_failing_scan_is_recorded_and_not_raised():
    def scan():
        raise FileNotFoundError("lsblk: not found")

    w, events = _watcher(scan)
    await w.start(use_udev=False)  # no exception: the app starts without detection
    assert events == []
    assert "lsblk" in w.status["scan_error"]


async def test_failing_scan_does_not_report_known_disks_removed():
    scan = Scan(disk("sda"))
    w, events = _watcher(scan)
    await w.start(use_udev=False)
    scan.disks = None  # next call raises TypeError
    await w.rescan()
    assert events == [("added", "sda")]


async def test_udev_monitor_that_cannot_start_is_recorded(monkeypatch):
    class Monitor:
        @staticmethod
        def from_netlink(context):
            raise OSError("netlink: permission denied")

    fake = types.SimpleNamespace(Context=lambda: None, Monitor=Monitor)
    monkeypatch.setitem(sys.modules, "pyudev", fake)
    w, _ = _watcher(Scan(disk("sda")))
    await w.start(use_udev=True)
    assert w.status["udev"].startswith("unavailable: netlink")


async def test_diagnostics_lists_every_disk_with_a_reason(env, monkeypatch):
    app, c = env
    nvme = BlockDeviceInfo(name="nvme0n1", path="/dev/nvme0n1", type="disk", tran="nvme")
    usb = disk("sdz", serial="S9", vendor="Zorvex", model="FlashStick")
    app.watcher.scan = Scan(usb, nvme)
    await app.watcher.rescan()

    async def no_sudo() -> bool:
        return False

    monkeypatch.setattr(facade_mod, "_sudo_works", no_sudo)
    monkeypatch.setattr(facade_mod.os, "geteuid", lambda: 1000)
    r = await c.get("/api/v1/devices/diagnostics")
    assert r.status_code == 200, r.text
    d = r.json()
    by_disk = {x["disk"]: x for x in d["disks"]}
    assert by_disk["/dev/sdz"]["state"] == "unknown"
    assert by_disk["/dev/sdz"]["model"] == "Zorvex FlashStick"
    assert by_disk["/dev/nvme0n1"]["state"] == "ignored"
    assert "not a USB disk" in by_disk["/dev/nvme0n1"]["reason"]
    assert d["privileges"] == "none"
    assert any("cannot be mounted" in h for h in d["hints"])

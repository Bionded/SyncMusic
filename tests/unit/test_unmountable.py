"""A connected disk whose file system does not mount: dirty NTFS after an unplug without eject."""
# ruff: noqa: F811 - pytest fixtures imported from test_api_sync

from __future__ import annotations

import pytest

from syncmusic.core.detection import ConnectedDisk
from syncmusic.core.models import DeviceConfig, DeviceIdentity
from syncmusic.devices import flashdrive
from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.guard import GuardError
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.unit.test_api_sync import ALL, env, music  # noqa: F401

DIRTY = ['[ 812.1] ntfs3(sdd1): volume is dirty and "force" flag is not set!']


class Guard:
    def check(self, *a, **kw):
        return None


async def test_mount_failure_is_unmountable_with_a_human_message(monkeypatch, tmp_path):
    async def run(cmd, **kw):
        if cmd[0] == "mount":
            raise mnt.BlockdevError("mount -t ntfs3 ... failed (32): wrong fs type, bad superblock")
        return ""

    node = BlockDeviceInfo(name="sdd1", path="/dev/sdd1", fstype="ntfs")
    monkeypatch.setattr(mnt, "run_cmd", run)
    monkeypatch.setattr(mnt, "mount_table", lambda: [])
    monkeypatch.setattr(mnt, "_node", lambda p, disks: node)
    monkeypatch.setattr(mnt, "scan_block_devices", lambda *_: [])
    monkeypatch.setattr(mnt, "_kernel_messages", lambda dev: DIRTY)
    with pytest.raises(mnt.UnmountableError, match="not ejected safely.*chkdsk"):
        await mnt.mount(Guard(), "/dev/sdd1", "flash", tmp_path)
    monkeypatch.setattr(mnt, "_kernel_messages", lambda dev: [])
    with pytest.raises(mnt.UnmountableError, match="does not mount.*bad superblock"):
        await mnt.mount(Guard(), "/dev/sdd1", "flash", tmp_path)
    blank = BlockDeviceInfo(name="sdd1", path="/dev/sdd1", fstype=None)
    monkeypatch.setattr(mnt, "_node", lambda p, disks: blank)
    with pytest.raises(mnt.UnmountableError, match="no file system"):
        await mnt.mount(Guard(), "/dev/sdd1", "flash", tmp_path)


async def test_no_file_system_yet_is_read_again_after_udev_settles(monkeypatch, tmp_path):
    """right after a format udev may still re-probe the partition, so lsblk shows no fstype yet."""
    ran: list[list[str]] = []

    async def run(cmd, **kw):
        ran.append(cmd)
        return ""

    nodes = iter(
        [
            BlockDeviceInfo(name="sdd1", path="/dev/sdd1", fstype=None),
            BlockDeviceInfo(name="sdd1", path="/dev/sdd1", fstype="vfat"),
        ]
    )
    monkeypatch.setattr(mnt, "run_cmd", run)
    monkeypatch.setattr(mnt, "mount_table", lambda: [])
    monkeypatch.setattr(mnt, "_node", lambda p, disks: next(nodes))
    monkeypatch.setattr(mnt, "scan_block_devices", lambda *_: [])
    m = await mnt.mount(Guard(), "/dev/sdd1", "flash", tmp_path)
    assert m.fstype == "vfat"
    settle = ["udevadm", "settle", "--timeout=10"]
    assert settle in ran and ran.index(settle) < [c[0] for c in ran].index("mount")


async def test_partition_missing_from_lsblk_is_checked_again_after_udev_settles(
    monkeypatch, tmp_path
):
    """udev re-probing a disk briefly drops its partitions from lsblk; the guard's lookup waits once."""
    ran: list[list[str]] = []

    async def run(cmd, **kw):
        ran.append(cmd)
        return ""

    class Flaky:
        checks = 0

        def check(self, *a, **kw):
            self.checks += 1
            if self.checks == 1:
                raise GuardError("refusing mount on /dev/sdd1: unknown block device")

    node = BlockDeviceInfo(name="sdd1", path="/dev/sdd1", fstype="vfat")
    monkeypatch.setattr(mnt, "run_cmd", run)
    monkeypatch.setattr(mnt, "mount_table", lambda: [])
    monkeypatch.setattr(mnt, "_node", lambda p, disks: node)
    monkeypatch.setattr(mnt, "scan_block_devices", lambda *_: [])
    guard = Flaky()
    await mnt.mount(guard, "/dev/sdd1", "flash", tmp_path)
    assert guard.checks == 2 and ran[0] == ["udevadm", "settle", "--timeout=10"]

    class Refusing:
        def check(self, *a, **kw):
            raise GuardError("refusing mount on /dev/sdd1: not in the allowlist")

    ran.clear()
    with pytest.raises(GuardError, match="allowlist"):
        await mnt.mount(Refusing(), "/dev/sdd1", "flash", tmp_path)
    assert ran == []  # any other refusal stands at once


async def _disk_device(app, on_connect="auto"):
    """A flash drive registered by disk (no mount_path) and connected as /dev/sdz."""
    cfg = DeviceConfig(
        id="zorvex",
        type="flashdrive",
        name="Zorvex",
        identity=DeviceIdentity(usb_serial="S1", manifest_device_id="u1"),
        rule_set={"template_ids": ["default.music.all"]},
        on_connect=on_connect,
    )
    await app.store.put_device(cfg)
    await app.sync.load()
    disk = BlockDeviceInfo(name="sdz", path="/dev/sdz", serial="S1")
    cd = ConnectedDisk(disk=disk, device_id="zorvex", data_partition="/dev/sdz1")
    app.sync.detector.connected["sdz"] = cd
    return cd


async def test_dirty_volume_reported_as_409_event_and_device_error(env, monkeypatch):
    app, c = env
    message = "/dev/sdz1: the NTFS volume is marked dirty (it was not ejected safely)."

    async def mount(*a, **kw):
        raise mnt.UnmountableError(message)

    monkeypatch.setattr(mnt, "mount", mount)
    cd = await _disk_device(app)
    q = app.bus.subscribe()
    await app.sync.on_disk_added(cd)  # on_connect auto
    events = [q.get_nowait() for _ in range(q.qsize())]
    err = next(e for e in events if e.type == "device.error")
    assert err.data["device_id"] == "zorvex" and err.data["code"] == "unmountable"
    assert "not ejected safely" in err.data["error"]
    r = await c.post("/api/v1/devices/zorvex/plan")
    assert r.status_code == 409 and r.json()["error"] == "unmountable"
    devices = (await c.get("/api/v1/devices")).json()["registered"]
    assert "not ejected safely" in next(d for d in devices if d["id"] == "zorvex")["error"]
    await app.sync.on_disk_removed("sdz")
    devices = (await c.get("/api/v1/devices")).json()["registered"]
    assert next(d for d in devices if d["id"] == "zorvex")["error"] is None


async def test_format_does_not_mount_the_old_file_system(env, monkeypatch, tmp_path):
    app, c = env
    calls: list[tuple] = []

    async def broken_mount(*a, **kw):  # the old fs: dirty
        raise mnt.UnmountableError("/dev/sdz1: dirty")

    async def format_disk(guard, disk_path, fs, label, registered=None):
        assert guard is app.guard
        calls.append(("format_disk", disk_path, fs, label))
        return "/dev/sdz1"

    async def mount_new(guard, part, device_id, root, **kw):
        calls.append(("mount", part))
        path = tmp_path / "mnt" / device_id
        path.mkdir(parents=True, exist_ok=True)
        return mnt.Mounted(path, part, "exfat", owned=True)

    new = BlockDeviceInfo(name="sdz1", path="/dev/sdz1", fstype="exfat", uuid="NEW-UUID")
    monkeypatch.setattr(mnt, "mount", broken_mount)
    monkeypatch.setattr(flashdrive, "format_disk", format_disk)
    monkeypatch.setattr(flashdrive, "mount", mount_new)
    monkeypatch.setattr(flashdrive, "scan_block_devices", lambda *_: [])
    monkeypatch.setattr(flashdrive, "find_disk_of", lambda p, disks: (new, new))
    await _disk_device(app, on_connect="manual")
    assert (await c.post("/api/v1/devices/zorvex/plan")).status_code == 409
    r = await c.post("/api/v1/devices/zorvex/format", json={"confirm": "Zorvex", "fs": "exfat"})
    assert r.status_code == 200, r.text
    assert calls == [("format_disk", "/dev/sdz", "exfat", "Zorvex"), ("mount", "/dev/sdz1")]
    assert (await app.sync.config("zorvex")).identity.fs_uuid == "NEW-UUID"
    monkeypatch.setattr(mnt, "is_live", lambda m: True)  # the fake mount counts as mounted
    device = await app.sync.open_device("zorvex")  # uses the new mount, no remount
    assert device.mount == tmp_path / "mnt" / "zorvex"
    devices = (await c.get("/api/v1/devices")).json()["registered"]
    assert next(d for d in devices if d["id"] == "zorvex")["error"] is None

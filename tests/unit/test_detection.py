"""udev watcher + device identification / matching."""

from __future__ import annotations

import asyncio

import pytest

from syncmusic.core import manifest as mf
from syncmusic.core.detection import Detector, match_registered, probe_disk
from syncmusic.core.models import DeviceConfig, DeviceIdentity, Manifest, ManifestHeader
from syncmusic.infra.blockdev.identify import BlockDeviceInfo, scan_block_devices
from syncmusic.infra.blockdev.watcher import BlockWatcher
from syncmusic.infra.events import EventBus


def disk(name="sda", serial="S1", uuid="AB-CD", tran="usb", fstype="vfat", mountpoints=None, **kw):
    part = BlockDeviceInfo(
        name=f"{name}1",
        path=f"/dev/{name}1",
        type="part",
        fstype=fstype,
        uuid=uuid,
        mountpoints=mountpoints or [],
    )
    return BlockDeviceInfo(
        name=name, path=f"/dev/{name}", type="disk", tran=tran, serial=serial, children=[part], **kw
    )


class Scan:
    def __init__(self, *disks):
        self.disks = list(disks)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return list(self.disks)


def _watcher(scan, debounce=0.05):
    events: list[tuple[str, str]] = []

    async def added(d):
        events.append(("added", d.name))

    async def removed(name):
        events.append(("removed", name))

    return BlockWatcher(added, removed, scan=scan, debounce_s=debounce), events


async def test_startup_scan_reports_usb_disks_only():
    nvme = BlockDeviceInfo(name="nvme0n1", path="/dev/nvme0n1", type="disk", tran="nvme")
    loop = BlockDeviceInfo(name="loop0", path="/dev/loop0", type="loop", fstype="swap")
    w, events = _watcher(Scan(disk("sda"), disk("sdb", serial="S2"), nvme, loop))
    await w.start(use_udev=False)
    assert events == [("added", "sda"), ("added", "sdb")]


def test_emulated_loop_disk_only_when_loop_devices_are_on():
    """an emulated device (loop image with a USB identity) is never picked up by a normal instance."""
    emu = disk("loop3", serial="EMU0000000FLASH1")
    emu.type = "loop"
    w, _ = _watcher(Scan())
    assert not w.wanted(emu)
    w.include_loop = True
    assert w.wanted(emu)


async def test_debounce_collapses_burst_and_detects_changes():
    scan = Scan()
    w, events = _watcher(scan)
    await w.start(use_udev=False)
    calls = scan.calls
    scan.disks = [disk("sda")]
    for action in ("add", "add", "change", "add"):  # disk + partitions arrive in a burst
        w.handle_uevent(action, "sda")
        await asyncio.sleep(0.01)
    await w.settle()
    assert events == [("added", "sda")] and scan.calls == calls + 1
    w.handle_uevent("change", "sda")  # nothing changed: no event
    await w.settle()
    assert events == [("added", "sda")]
    scan.disks = [disk("sda", uuid="NEW-UUID", fstype="exfat")]  # reformatted
    w.handle_uevent("change", "sda")
    await w.settle()
    assert events[-1] == ("added", "sda")
    scan.disks = []
    w.handle_uevent("remove", "sda")
    await w.settle()
    assert events[-1] == ("removed", "sda")
    await w.stop()


async def test_callback_error_does_not_stop_watcher():
    async def bad(d):
        raise RuntimeError("boom")

    removed = []

    async def rm(n):
        removed.append(n)

    scan = Scan(disk("sda"))
    w = BlockWatcher(bad, rm, scan=scan, debounce_s=0.01)
    await w.start(use_udev=False)
    scan.disks = []
    w.handle_uevent("remove", "sda")
    await w.settle()
    assert removed == ["sda"]


def _cfg(id_, type_="flashdrive", **identity) -> DeviceConfig:
    return DeviceConfig(id=id_, type=type_, name=id_, identity=DeviceIdentity(**identity))


def test_matching_order_serial_manifest_uuid():
    by_serial = _cfg("a", usb_serial="S1")
    by_manifest = _cfg("b")
    by_uuid = _cfg("c", fs_uuid="AB-CD")
    d = disk()
    assert match_registered(d, [by_uuid, by_manifest, by_serial], "b") == (by_serial, "usb_serial")
    assert match_registered(disk(serial=None), [by_uuid, by_manifest], "b") == (
        by_manifest,
        "manifest",
    )
    assert match_registered(disk(serial=None), [by_uuid, by_manifest], None) == (by_uuid, "fs_uuid")
    assert match_registered(disk(serial="X", uuid="Y"), [by_uuid, by_serial], None) is None
    renamed = _cfg("d", manifest_device_id="old-id")
    assert match_registered(disk(serial=None), [renamed], "old-id") == (renamed, "manifest")


def test_unknown_disk_probed_by_every_class(monkeypatch):
    from syncmusic.base.device import ProbeResult
    from syncmusic.core import registry

    class Low:
        @classmethod
        def probe(cls, blk, mount):
            return ProbeResult(type_name="low", confidence=10)

    class High:
        @classmethod
        def probe(cls, blk, mount):
            return ProbeResult(type_name="high", confidence=90, data_partition="/dev/sda1")

    class Broken:
        @classmethod
        def probe(cls, blk, mount):
            raise RuntimeError("bug")

    class No:
        @classmethod
        def probe(cls, blk, mount):
            return None

    monkeypatch.setattr(registry, "device_classes", lambda: [Low, Broken, High, No])
    assert [r.type_name for r in probe_disk(disk())] == ["high", "low"]


async def test_detector_events(tmp_path):
    bus = EventBus()
    q = bus.subscribe()
    configs = [_cfg("flash1", usb_serial="S1")]
    det = Detector(bus, lambda: configs)
    cd = await det.disk_added(disk())
    assert (
        cd.device_id == "flash1"
        and cd.matched_by == "usb_serial"
        and cd.data_partition == "/dev/sda1"
    )
    ev = q.get_nowait()
    assert ev.type == "device.connected" and ev.data["device_id"] == "flash1"
    assert det.by_device("flash1") is cd

    unknown = await det.disk_added(disk("sdb", serial="ZZ", uuid="11-22"))
    ev = q.get_nowait()
    assert unknown.device_id is None and ev.type == "device.unknown"
    assert (
        ev.data["probes"][0]["type_name"] == "flashdrive"
    )  # the flash drive class claims any USB disk
    assert unknown.data_partition == "/dev/sdb1"

    await det.disk_added(disk())  # re-reported after a change (format)
    assert q.get_nowait().type == "device.changed"
    await det.disk_removed("sda")
    ev = q.get_nowait()
    assert ev.type == "device.removed" and ev.data["device_id"] == "flash1"
    await det.disk_removed("nope")
    assert q.empty()


async def test_manifest_id_from_existing_mount(tmp_path):
    mf.write(tmp_path, Manifest(header=ManifestHeader(device_id="flash9")))
    configs = [_cfg("flash9")]
    det = Detector(EventBus(), lambda: configs)
    cd = await det.disk_added(disk(serial=None, uuid=None, mountpoints=[str(tmp_path)]))
    assert cd.device_id == "flash9" and cd.matched_by == "manifest" and cd.mountpoint == tmp_path


@pytest.mark.integration
async def test_real_startup_scan_and_udev_monitor():
    """Read-only: the real scan reports USB disks only and the pyudev monitor starts and stops. Disks may come
    and go meanwhile (emulated devices of a parallel test): only those present throughout must match. Emulated
    loop disks also report usb but the watcher skips them unless loop devices are on: leave them out."""

    def usb() -> set[str]:
        return {d.name for d in scan_block_devices() if d.tran == "usb" and d.type != "loop"}

    before = usb()
    w, events = _watcher(scan_block_devices, debounce=0.01)
    await w.start(use_udev=True)
    try:
        names = {n for _, n in events}
        assert not any(n.startswith(("nvme", "mmcblk", "zram")) for n in names)
        after = usb()
        assert before & after <= names <= before | after
    finally:
        await w.stop()


def test_a_serial_alias_matches_like_the_serial():
    """the iPod mini in Apple disk mode reports another USB serial than under Rockbox."""
    mini = _cfg("mini", usb_serial="ROCKBOXMODE", usb_serial_aliases=["DISKMODE"])
    other = _cfg("other", usb_serial="OTHER")
    assert match_registered(disk(serial="DISKMODE"), [other, mini], None) == (mini, "usb_serial")
    assert match_registered(disk(serial="ROCKBOXMODE"), [other, mini], None) == (mini, "usb_serial")
    assert match_registered(disk(serial="NOPE", uuid="Y"), [other, mini], None) is None

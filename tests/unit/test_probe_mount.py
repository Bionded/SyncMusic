"""Read-only probe mounts of unknown disks to read the manifest id."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from syncmusic.core import manifest as mf
from syncmusic.core.detection import Detector, MountedProbe, manifest_id_at
from syncmusic.core.models import DeviceConfig, DeviceIdentity, Manifest, ManifestHeader
from syncmusic.core.sync import SyncService
from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.guard import GuardError
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from syncmusic.infra.events import EventBus

from .test_detection import disk
from .test_stale_mounts import _kernel


def _cfg(id_, **identity) -> DeviceConfig:
    return DeviceConfig(id=id_, type="flashdrive", name=id_, identity=DeviceIdentity(**identity))


class Probe:
    def __init__(self, result=None, error=None, probes=()):
        self.result, self.error, self.calls = result, error, []
        self.probes = list(probes)

    async def __call__(self, d):
        self.calls.append(d.name)
        if self.error:
            raise self.error
        return MountedProbe(self.result, self.probes)


def _detector(configs, probe):
    return Detector(EventBus(), lambda: configs, probe), None


async def test_unknown_unmounted_disk_is_probed_and_matched_by_manifest():
    probe = Probe("uid-1")
    det, _ = _detector([_cfg("flash", manifest_device_id="uid-1", fs_uuid="OLD")], probe)
    cd = await det.disk_added(disk(serial="OTHER", uuid="NEW-AFTER-FORMAT"))
    assert probe.calls == ["sda"]
    assert (cd.device_id, cd.matched_by) == ("flash", "manifest")


async def test_serial_match_and_mounted_disks_are_not_probed(tmp_path):
    probe = Probe("uid-1")
    det, _ = _detector([_cfg("flash", usb_serial="S1")], probe)
    cd = await det.disk_added(disk(serial="S1"))
    assert cd.matched_by == "usb_serial" and probe.calls == []
    # an already mounted disk: the manifest is read from its mountpoint, no probe mount
    await det.disk_added(disk(name="sdb", serial="X", mountpoints=[str(tmp_path)]))
    assert probe.calls == []


async def test_failed_probe_leaves_disk_unknown():
    probe = Probe(error=RuntimeError("mount failed"))
    det, _ = _detector([_cfg("flash", manifest_device_id="uid-1")], probe)
    cd = await det.disk_added(disk(serial="OTHER"))
    assert cd.device_id is None and probe.calls == ["sda"]


async def test_probe_read_mounts_read_only_and_unmounts(monkeypatch, tmp_path):
    k = _kernel(monkeypatch, [])
    checks = []

    class Guard:
        def check(self, target, operation, registered=None):
            checks.append((target, operation))

    node = BlockDeviceInfo(name="sdd1", path="/dev/sdd1", fstype="vfat")
    monkeypatch.setattr(mnt, "_node", lambda p, disks: node)
    monkeypatch.setattr(mnt, "scan_block_devices", lambda *_: [])
    seen = []
    result = await mnt.probe_read(
        Guard(), "/dev/sdd1", tmp_path, lambda p: seen.append(p) or "id-7"
    )
    target = str(tmp_path / ".probe-sdd1")
    assert result == "id-7" and seen == [tmp_path / ".probe-sdd1"]
    assert checks == [("/dev/sdd1", "mount")]
    mount_cmd = next(c for c in k.cmds if c[0] == "mount")
    opts = mount_cmd[mount_cmd.index("-o") + 1].split(",")
    assert "ro" in opts and "rw" not in opts
    assert ["umount", target] in k.cmds
    assert k.cmds.index(mount_cmd) < k.cmds.index(["umount", target])
    assert ["rmdir", target] in k.cmds


async def test_probe_read_unmounts_when_read_fails(monkeypatch, tmp_path):
    k = _kernel(monkeypatch, [])

    class Guard:
        def check(self, *a, **kw):
            return None

    monkeypatch.setattr(
        mnt, "_node", lambda p, disks: BlockDeviceInfo(name="sdd1", path="/dev/sdd1", fstype="vfat")
    )
    monkeypatch.setattr(mnt, "scan_block_devices", lambda *_: [])

    def boom(p):
        raise OSError("read error")

    with pytest.raises(OSError):
        await mnt.probe_read(Guard(), "/dev/sdd1", tmp_path, boom)
    assert ["umount", str(tmp_path / ".probe-sdd1")] in k.cmds


async def test_guard_refusal_means_no_mount(monkeypatch, tmp_path):
    k = _kernel(monkeypatch, [])

    class Guard:
        def check(self, target, operation, registered=None):
            raise GuardError(f"refusing {operation} on {target}: transport is 'nvme'")

    with pytest.raises(GuardError):
        await mnt.probe_read(Guard(), "/dev/nvme0n1p2", tmp_path, manifest_id_at)
    assert k.cmds == []


def _service(tmp_path, detect=True):
    ctx = SimpleNamespace(
        guard=object(),
        bus=EventBus(),
        config=SimpleNamespace(mount_root=tmp_path, detect_devices=detect),
    )
    return SyncService(ctx, None, None, None)  # type: ignore[arg-type]


async def test_service_probe_requires_same_disk_and_detection(monkeypatch, tmp_path):
    from syncmusic.core import sync as sync_mod

    calls = []

    async def fake_probe_read(guard, path, root, read):
        calls.append(path)
        return MountedProbe("uid-1")

    monkeypatch.setattr(mnt, "probe_read", fake_probe_read)
    reported = disk(serial="S9", uuid="U1")

    monkeypatch.setattr(sync_mod, "scan_block_devices", lambda *_: [disk(serial="S9", uuid="U1")])
    assert (await _service(tmp_path)._probe_manifest(reported)).manifest_id == "uid-1"
    assert calls == ["/dev/sda1"]

    calls.clear()
    assert await _service(tmp_path, detect=False)._probe_manifest(reported) is None
    monkeypatch.setattr(
        sync_mod, "scan_block_devices", lambda *_: [disk(serial="OTHER", uuid="U1")]
    )
    assert await _service(tmp_path)._probe_manifest(reported) is None
    monkeypatch.setattr(
        sync_mod, "scan_block_devices", lambda *_: [disk(serial="S9", uuid="CHANGED")]
    )
    assert await _service(tmp_path)._probe_manifest(reported) is None
    assert calls == []


def test_manifest_id_at_reads_written_manifest(tmp_path):
    mf.write(tmp_path, Manifest(header=ManifestHeader(device_id="flash", device_uid="uid-1")))
    assert manifest_id_at(tmp_path) == "uid-1"
    assert manifest_id_at(tmp_path / "nothing") is None


# --- an unknown iPod is identified from its content while probe-mounted ----------------------


def _apple(serial="000A27"):
    fw = BlockDeviceInfo(name="sdc1", path="/dev/sdc1", type="part", size=100_000_000)
    data = BlockDeviceInfo(
        name="sdc2", path="/dev/sdc2", type="part", fstype="vfat", uuid="D802-5614", label="MINI"
    )
    return BlockDeviceInfo(
        name="sdc",
        path="/dev/sdc",
        type="disk",
        tran="usb",
        serial=serial,
        usb_vendor_id="05ac",
        children=[fw, data],
    )


async def _probe_tree(monkeypatch, tmp_path, *dirs):
    """_probe_manifest of an unknown Apple disk whose data partition holds `dirs` (probe_read stubbed)."""
    from syncmusic.core import sync as sync_mod

    tree = tmp_path / "tree"
    for d in dirs:
        (tree / d).mkdir(parents=True)

    async def fake_probe_read(guard, path, root, read):
        assert path == "/dev/sdc2"
        return read(tree)

    monkeypatch.setattr(mnt, "probe_read", fake_probe_read)
    monkeypatch.setattr(sync_mod, "scan_block_devices", lambda *_: [_apple()])
    service = _service(tmp_path)
    det = Detector(service.ctx.bus, lambda: [], service._probe_manifest)
    return await det.disk_added(_apple())


async def test_unknown_rockbox_ipod_is_offered_as_rockbox(monkeypatch, tmp_path):
    cd = await _probe_tree(monkeypatch, tmp_path, ".rockbox", "Music")
    assert cd.device_id is None
    # Rockbox first; the Apple firmware it dual-boots second
    assert [(p.type_name, p.confidence) for p in cd.probes] == [("rockbox", 80), ("ipod", 30)]
    assert cd.probes[1].details == {"rockbox": True}
    assert cd.probes[0].confidence == 80 and cd.probes[0].identity.usb_serial == "000A27"


async def test_unknown_stock_ipod_is_offered_as_ipod(monkeypatch, tmp_path):
    cd = await _probe_tree(monkeypatch, tmp_path, "iPod_Control/iTunes")
    assert cd.probes[0].type_name == "ipod" and cd.probes[0].confidence == 60
    assert cd.probes[0].identity.fs_uuid == "D802-5614"
    assert cd.data_partition == "/dev/sdc2"


async def test_unknown_ipod_without_content_or_mount_keeps_disk_probes(monkeypatch, tmp_path):
    cd = await _probe_tree(monkeypatch, tmp_path)  # empty data partition: nothing claims the tree
    assert [(p.type_name, p.confidence) for p in cd.probes] == [("ipod", 40), ("rockbox", 30)]
    det = Detector(EventBus(), lambda: [], Probe(error=GuardError("refused")))
    cd = await det.disk_added(_apple())
    assert [(p.type_name, p.confidence) for p in cd.probes] == [("ipod", 40), ("rockbox", 30)]

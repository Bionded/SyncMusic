"""Probe mount scenario: run on emulated players (tests/integration) and on real ones."""

from __future__ import annotations

import asyncio
import shutil
import time
from types import SimpleNamespace

from syncmusic.core import manifest as mf
from syncmusic.core.detection import Detector
from syncmusic.core.models import DeviceConfig, DeviceIdentity, Manifest, ManifestHeader
from syncmusic.core.sync import SyncService
from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.identify import scan_block_devices
from syncmusic.infra.events import EventBus


def _flash(serial):
    for d in scan_block_devices(True):
        if d.serial == serial and d.tran == "usb":
            return d
    return None


async def probe_manifest_scenario(tmp_path, serial, guard):
    """Matched by a probe-mounted manifest (also on an emulated flash drive, test_emulated_flash.py)."""
    disk = _flash(serial)
    root = tmp_path / "mnt"
    root.mkdir(parents=True, exist_ok=True)  # ours, not created by root (pytest can clean it up)
    # the new file system's type reaches the scan once udev has probed it (slower while other tests make disks)
    deadline = time.monotonic() + 30
    while not (parts := [c for c in disk.children if c.fstype in mnt.MOUNT_TYPES]):
        assert time.monotonic() < deadline, (
            f"no mountable partition on {disk.path}: {disk.children}"
        )
        await asyncio.sleep(0.5)
        disk = _flash(serial) or disk
    part = parts[0]
    await mnt.unmount_all(disk)

    m = await mnt.mount(guard, part.path, "hw-probe-setup", root, registered=[serial])
    had_dir = (m.path / ".syncmusic").exists()
    backup = None
    if (m.path / mf.MANIFEST_PATH).exists():
        backup = (m.path / mf.MANIFEST_PATH).read_bytes()
    mf.write(
        m.path, Manifest(header=ManifestHeader(device_id="zorvex", device_uid="uid-probe-stale"))
    )
    await mnt.unmount(m)
    try:
        cfg = DeviceConfig(
            id="zorvex",
            type="flashdrive",
            name="Zorvex",
            identity=DeviceIdentity(fs_uuid="STALE-UUID", manifest_device_id="uid-probe-stale"),
        )
        ctx = SimpleNamespace(
            guard=guard,
            bus=EventBus(),
            config=SimpleNamespace(mount_root=root, detect_devices=True),
        )
        service = SyncService(ctx, None, None, None)  # type: ignore[arg-type]
        det = Detector(ctx.bus, lambda: [cfg], service._probe_manifest)
        cd = await det.disk_added(_flash(serial))
        assert (cd.device_id, cd.matched_by) == ("zorvex", "manifest")
        assert not any(
            ".probe-" in e.path and e.path.startswith(str(root)) for e in mnt.mount_table()
        )  # ours only: tests on other workers probe too
        assert not (root / f".probe-{part.name}").exists()
        assert not _flash(serial).all_mountpoints()
    finally:
        m = await mnt.mount(guard, part.path, "hw-probe-setup", root, registered=[serial])
        if backup is not None:
            (m.path / mf.MANIFEST_PATH).write_bytes(backup)
        elif not had_dir:
            shutil.rmtree(m.path / ".syncmusic")
        await mnt.unmount(m)


async def rockbox_probe_scenario(tmp_path, serial, guard):
    """An unregistered Rockbox player is offered as Rockbox (also emulated, test_emulated_rockbox.py)."""
    disk = next(d for d in scan_block_devices(True) if d.serial == serial)
    await mnt.unmount_all(disk)
    disk = next(d for d in scan_block_devices(True) if d.serial == serial)
    root = tmp_path / "mnt"
    root.mkdir(parents=True, exist_ok=True)  # ours, not created by root (pytest can clean it up)
    ctx = SimpleNamespace(
        guard=guard,
        bus=EventBus(),
        config=SimpleNamespace(mount_root=root, detect_devices=True),
    )
    service = SyncService(ctx, None, None, None)  # type: ignore[arg-type]
    cd = await Detector(ctx.bus, lambda: [], service._probe_manifest).disk_added(disk)
    assert cd.device_id is None
    assert cd.probes[0].type_name == "rockbox" and cd.probes[0].confidence == 80
    assert cd.probes[0].identity.usb_serial == serial
    assert cd.data_partition == next(c.path for c in disk.children if c.fstype == "vfat")
    assert not any(
        ".probe-" in e.path and e.path.startswith(str(root)) for e in mnt.mount_table()
    )  # ours only: tests on other workers probe too

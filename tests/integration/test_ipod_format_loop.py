"""IPod.format on an emulated stock iPod mini 2G (loop image, tests/emulated.py): type-0 firmware partition + FAT32 data
partition. Only the data partition is reformatted: partition table and firmware bytes stay identical."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from syncmusic.core.models import DeviceConfig
from syncmusic.devices.iPod.ipod import IPod
from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.identify import find_disk_of, scan_block_devices
from tests.emulated import (
    MINI_LAYOUT,
    attach,
    emulated_guard,
    needs_sudo,
    serial_of,
    sh,
    write_firmware,
)

pytestmark = [pytest.mark.integration, pytest.mark.emulated]

IMAGE_MB = 96
FW_SECTORS = 16384  # 8 MiB firmware partition at sector 2048
SYSINFO = b"ModelNumStr: xA204\nFirewireGuid: 0x000A1B2C3D4E5F60\n"


@pytest.fixture
def ipod_loop():
    with attach("emulated_ipod_mini", IMAGE_MB, layout=MINI_LAYOUT) as disk:
        write_firmware(disk)  # recognisable firmware bytes
        sh("mkfs.vfat", "-F", "32", "-n", "OLD", disk.part(2))
        yield disk.dev, disk.image


def _protected(img) -> str:
    """sha256 of everything before the data partition: MBR, gap, firmware partition."""
    with open(img, "rb") as f:
        return hashlib.sha256(f.read((2048 + FW_SECTORS) * 512)).hexdigest()


@needs_sudo
async def test_format_keeps_partition_table_and_firmware(ipod_loop, tmp_path):
    from pygpod import Database, init_ipod

    dev, img = ipod_loop
    guard = emulated_guard()
    root = tmp_path / "mnt"
    # an existing iPod with SysInfo and some junk
    m = await mnt.mount(guard, f"{dev}p2", "setup", root)
    init_ipod(str(m.path), name="Old", sysinfo_files={"SysInfo": SYSINFO})
    (m.path / "iPod_Control" / "Device" / "Preferences").write_bytes(b"\x01language=ru")
    (m.path / "iPod_Control" / "Music" / "F00" / "JUNK.mp3").write_bytes(b"x" * 1000)
    await mnt.unmount(m)
    old_uuid = find_disk_of(f"{dev}p2", scan_block_devices(False))[1].uuid
    before = _protected(img)

    ctx = SimpleNamespace(guard=guard, config=SimpleNamespace(mount_root=root))
    ipod = IPod(DeviceConfig(id="mini", type="ipod", name="Мій iPod"), root / "mini", ctx)  # type: ignore[arg-type]
    ipod.block_path = dev
    try:
        part = await ipod.format("Мій iPod")
        assert part == f"{dev}p2"
        assert ipod.mount == root / "mini"
        assert ipod.config.identity.fs_uuid and ipod.config.identity.fs_uuid != old_uuid
        db = Database(str(ipod.mount))
        assert db.master_playlist.name == "Мій iPod" and db.tracks == []
        assert (ipod.mount / "iPod_Control" / "Device" / "SysInfo").read_bytes() == SYSINFO
        assert (
            ipod.mount / "iPod_Control" / "Device" / "Preferences"
        ).read_bytes() == b"\x01language=ru"
        assert not (ipod.mount / "iPod_Control" / "Music" / "F00" / "JUNK.mp3").exists()
        assert IPod.probe(None, ipod.mount) is not None
        assert await ipod.read_manifest() is None
    finally:
        await mnt.unmount(mnt.Mounted(ipod.mount, f"{dev}p2", "vfat", owned=True))
    assert _protected(img) == before
    disk, _ = find_disk_of(dev, scan_block_devices(False))
    assert [p.parttype for p in disk.partitions()] == ["0x0", "0xb"]


@needs_sudo
async def test_format_without_readable_sysinfo_uses_usb_guid(ipod_loop, tmp_path):
    """A data partition that does not mount (corrupt) is still formatted; SysInfo comes from the serial."""
    from pygpod import Database

    dev, img = ipod_loop
    sh("dd", "if=/dev/zero", f"of={dev}p2", "bs=1M", "count=1", "conv=fsync")  # destroy the FAT
    sh("udevadm", "settle", "--timeout=10")
    guard = emulated_guard()
    root = tmp_path / "mnt"
    ctx = SimpleNamespace(guard=guard, config=SimpleNamespace(mount_root=root))
    ipod = IPod(DeviceConfig(id="mini", type="ipod", name="X"), root / "mini", ctx)  # type: ignore[arg-type]
    ipod.block_path = dev
    try:
        await ipod.format("X")
        sysinfo = (ipod.mount / "iPod_Control" / "Device" / "SysInfo").read_bytes()
        assert (
            sysinfo == f"FirewireGuid: 0x{serial_of('emulated_ipod_mini')}\n".encode()
        )  # its USB serial
        assert Database(str(ipod.mount)).master_playlist.name == "X"
    finally:
        await mnt.unmount(mnt.Mounted(ipod.mount, f"{dev}p2", "vfat", owned=True))


@needs_sudo
async def test_rename_keeps_partition_table_and_firmware(ipod_loop, tmp_path):
    """Rename: the iPod's name in its database and the data partition's label; the firmware partition and
    the partition table stay byte-identical."""
    from pygpod import Database, init_ipod

    from syncmusic.infra.blockdev import format as fmt

    dev, img = ipod_loop
    guard = emulated_guard()
    root = tmp_path / "mnt"
    m = await mnt.mount(guard, f"{dev}p2", "mini", root)
    init_ipod(str(m.path), name="Old", sysinfo_files={"SysInfo": SYSINFO})
    before = _protected(img)
    ctx = SimpleNamespace(guard=guard, config=SimpleNamespace(mount_root=root))
    ipod = IPod(DeviceConfig(id="mini", type="ipod", name="Old"), m.path, ctx)  # type: ignore[arg-type]
    ipod.block_path = dev
    await ipod.rename("Мій iPod")
    _, node = find_disk_of(f"{dev}p2", scan_block_devices(False))
    label = ipod.label_for("Мій iPod", node.fstype, node.label)
    assert node.label == "OLD" and label == "IPOD"
    await fmt.set_label(guard, f"{dev}p2", "vfat", label)
    _, node = find_disk_of(f"{dev}p2", scan_block_devices(False))
    assert node.label == "IPOD"
    assert ipod.label_for("x", "vfat", None) is None  # no label before: none is added
    m = await mnt.mount(guard, f"{dev}p2", "mini", root)
    try:
        assert Database(str(m.path)).master_playlist.name == "Мій iPod"
    finally:
        await mnt.unmount(m)
    assert (
        _protected(img) == before
    )  # (the guard refuses labelling a real iPod's other nodes: test_guard)

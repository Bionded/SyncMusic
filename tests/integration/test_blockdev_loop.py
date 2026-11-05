"""Mount + format on emulated flash drives (loop images, tests/emulated.py). Needs passwordless
sudo and loop devices.

Destructive commands only ever run on the emulated loop devices created here; the real disks are only passed to the
guard (read-only check) - never to format/mount code.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from syncmusic.core.models import DeviceConfig
from syncmusic.devices.flashdrive import FlashDrive
from syncmusic.infra.blockdev import format as fmt
from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.guard import Guard, GuardError
from syncmusic.infra.blockdev.identify import find_disk_of, scan_block_devices
from tests.conftest import HARDWARE_TOML
from tests.emulated import attach, emulated_guard, needs_sudo, plain_loop, serial_of, sh

pytestmark = [pytest.mark.integration, pytest.mark.emulated]

IMAGE_MB = 128


@pytest.fixture
def loop_disk():
    with attach("emulated_flash", IMAGE_MB) as disk:
        yield disk.dev


@pytest.fixture
def guard():
    return emulated_guard()


def _options(mount: Path) -> list[str]:
    for line in Path("/proc/self/mounts").read_text().splitlines():
        fields = line.split()
        if fields[1] == str(mount):
            return fields[3].split(",")
    raise AssertionError(f"{mount} not mounted")


@needs_sudo
@pytest.mark.parametrize("fs", fmt.FILESYSTEMS)
async def test_format_mount_write_unmount(loop_disk, guard, tmp_path, fs):
    part = await fmt.format_disk(guard, loop_disk, fs, "Тест Flash")
    assert part == f"{loop_disk}p1"
    disk, node = find_disk_of(part, scan_block_devices(with_udev=False))
    assert node.fstype == ("ntfs" if fs == "ntfs" else fs) and len(disk.partitions()) == 1
    m = await mnt.mount(guard, part, "dev1", tmp_path / "mnt")
    try:
        assert m.owned and m.path == tmp_path / "mnt" / "dev1"
        # vfat's `flush` sleeps 100 ms on every close of a written file
        assert "flush" not in _options(m.path)
        name = "Скрябін - Група крові.mp3"
        (m.path / name).write_bytes(b"x" * 1000)  # writable by the app user
        assert (m.path / name).read_bytes() == b"x" * 1000
        again = await mnt.mount(
            guard, part, "other", tmp_path / "mnt"
        )  # reuse the existing mountpoint
        assert not again.owned and again.path == m.path
        await mnt.unmount(again)  # reused: stays mounted
        assert (m.path / name).exists()
    finally:
        await mnt.unmount(m)
    assert not (tmp_path / "mnt" / "dev1").exists()
    m = await mnt.mount(guard, part, "dev1", tmp_path / "mnt", read_only=True)
    try:
        assert (m.path / "Скрябін - Група крові.mp3").stat().st_size == 1000
    finally:
        await mnt.unmount(m)


@needs_sudo
@pytest.mark.parametrize("fs", fmt.FILESYSTEMS)
async def test_set_label_changes_only_the_label(loop_disk, guard, tmp_path, fs):
    """Rename: the label changes; partition table, file system UUID and files stay."""
    part = await fmt.format_disk(guard, loop_disk, fs, "OLD")
    m = await mnt.mount(guard, part, "dev1", tmp_path / "mnt")
    (m.path / "Скрябін.mp3").write_bytes(b"x" * 1000)
    table = sh("sfdisk", "--dump", loop_disk)
    uuid = find_disk_of(part, scan_block_devices(with_udev=False))[1].uuid
    label = await fmt.set_label(guard, part, fs, "Новий Flash-2")  # unmounts first
    assert not (tmp_path / "mnt" / "dev1").is_mount()
    _, node = find_disk_of(part, scan_block_devices(with_udev=False))
    assert label == fmt.clean_label("Новий Flash-2", fs) and node.label == label
    if fs == "vfat":
        assert label == "FLASH-2"  # upper-case ASCII
    assert node.uuid == uuid and sh("sfdisk", "--dump", loop_disk) == table
    m = await mnt.mount(guard, part, "dev1", tmp_path / "mnt")
    try:
        assert (m.path / "Скрябін.mp3").read_bytes() == b"x" * 1000
    finally:
        await mnt.unmount(m)


@needs_sudo
async def test_format_refuses_partition_and_mkfs_on_partition_works(loop_disk, guard):
    part = await fmt.format_disk(guard, loop_disk, "vfat", "A")
    with pytest.raises(GuardError, match="not a whole disk"):
        await fmt.format_disk(guard, part, "vfat", "B")
    await fmt.make_filesystem(guard, part, "exfat", "B")
    _, node = find_disk_of(part, scan_block_devices(with_udev=False))
    assert node.fstype == "exfat" and node.label == "B"


@needs_sudo
async def test_loop_refused_without_allow_loop(loop_disk, tmp_path):
    with pytest.raises(GuardError, match="loop"):
        await fmt.format_disk(Guard(HARDWARE_TOML), loop_disk, "vfat", "X")
    with pytest.raises(GuardError, match="loop"):
        await mnt.mount(Guard(HARDWARE_TOML), loop_disk, "d", tmp_path)


@needs_sudo
async def test_loop_refused_unless_in_the_emulated_allowlist(loop_disk, tmp_path):
    """allowing loop devices is not enough; the emulated device's serial must be allowlisted."""
    disk, _ = find_disk_of(loop_disk, scan_block_devices(with_udev=False))
    assert disk.serial == serial_of("emulated_flash") and disk.usb_vendor_id == "0951"
    with pytest.raises(GuardError, match="emulated-device allowlist"):
        await fmt.format_disk(Guard(HARDWARE_TOML, allow_loop=True), loop_disk, "vfat", "X")
    with pytest.raises(GuardError, match="emulated-device allowlist"):
        await fmt.format_disk(Guard(None, allow_loop=True), loop_disk, "vfat", "X")
    # a loop image without the identity sidecar
    with (
        plain_loop(tmp_path / "plain.img") as plain,
        pytest.raises(GuardError, match="emulated-device allowlist"),
    ):
        await fmt.format_disk(emulated_guard(), plain, "vfat", "X")


@needs_sudo
async def test_flashdrive_format_on_loop(loop_disk, guard, tmp_path):
    await fmt.format_disk(guard, loop_disk, "ext4", "OLD")
    m = await mnt.mount(guard, f"{loop_disk}p1", "flash1", tmp_path / "mnt")
    (m.path / "junk.mp3").write_bytes(b"x")
    ctx = SimpleNamespace(guard=guard, config=SimpleNamespace(mount_root=tmp_path / "mnt"))
    dev = FlashDrive(DeviceConfig(id="flash1", type="flashdrive", name="F"), m.path, ctx)  # type: ignore[arg-type]
    dev.block_path = loop_disk
    try:
        await dev.format("Мій Flash", "vfat")
        assert dev.mount == tmp_path / "mnt" / "flash1"
        assert not (dev.mount / "junk.mp3").exists()
        assert dev.effective_capabilities().path_rules.max_file_size == 4 * 1024**3 - 1
        assert FlashDrive.probe(None, dev.mount) is not None
        assert dev.config.identity.fs_uuid
        assert await dev.read_manifest() is None
    finally:
        await mnt.unmount(mnt.Mounted(dev.mount, f"{loop_disk}p1", "vfat", owned=True))


@needs_sudo
async def test_rockbox_format_on_loop_keeps_rockbox_dir(loop_disk, guard, tmp_path):
    """the data partition is reformatted, /.rockbox restored, the partition table kept."""
    from syncmusic.devices.Rockbox.rockbox import Rockbox
    from tests.contracts.harness import rockbox_tree

    await fmt.format_disk(guard, loop_disk, "vfat", "OLD")
    table = sh("sfdisk", "--dump", loop_disk)
    m = await mnt.mount(guard, f"{loop_disk}p1", "rb1", tmp_path / "mnt")
    rockbox_tree(m.path)
    (m.path / ".rockbox" / "codecs").mkdir()
    (m.path / ".rockbox" / "codecs" / "mpa.codec").write_bytes(b"codec")
    (m.path / "Music").mkdir()
    (m.path / "Music" / "junk.mp3").write_bytes(b"x")
    ctx = SimpleNamespace(
        guard=guard,
        config=SimpleNamespace(mount_root=tmp_path / "mnt", cache_dir=tmp_path / "cache"),
    )
    dev = Rockbox(DeviceConfig(id="rb1", type="rockbox", name="R"), m.path, ctx)  # type: ignore[arg-type]
    dev.block_path = loop_disk
    old_uuid = find_disk_of(f"{loop_disk}p1", scan_block_devices(with_udev=False))[1].uuid
    try:
        assert await dev.format("Мій Rockbox") == f"{loop_disk}p1"  # mounted: copied directly
        assert (dev.mount / ".rockbox" / "codecs" / "mpa.codec").read_bytes() == b"codec"
        # unmounted (as SyncService.format calls it): the backup goes through a read-only probe mount
        await mnt.unmount(mnt.Mounted(dev.mount, f"{loop_disk}p1", "vfat", owned=True))
        assert await dev.format("Мій Rockbox") == f"{loop_disk}p1"
        assert not (dev.mount / "Music").exists()
        assert (dev.mount / ".rockbox" / "codecs" / "mpa.codec").read_bytes() == b"codec"
        assert not list((tmp_path / "mnt").glob(".probe-*"))
        assert Rockbox.probe(None, dev.mount) is not None
        assert dev.effective_capabilities().formats == {"mp3"}
        assert dev.config.identity.fs_uuid and dev.config.identity.fs_uuid != old_uuid
        assert sh("sfdisk", "--dump", loop_disk) == table
        assert not any((tmp_path / "cache").iterdir())  # backup removed
    finally:
        await mnt.unmount(mnt.Mounted(dev.mount, f"{loop_disk}p1", "vfat", owned=True))


@pytest.fixture
def loop_4k():
    """A 512 MB emulated disk with 4096-byte logical sectors and one partition at sector 63 (like the Classic)."""
    layout = "label: dos\nstart=63, type=b\n"
    with attach("emulated_flash", IMAGE_MB * 4, layout=layout, sector_size=4096) as disk:
        yield disk.dev


@needs_sudo
async def test_vfat_on_4k_sector_disk_like_apple(loop_4k, guard):
    """on a 4096-byte-sector disk (the SSD-modded Classic) FAT32 gets Apple's 16 KiB clusters and
    hidden sectors in logical sectors, not mkfs.fat's 256 KiB clusters / 512-byte-unit hidden sectors."""
    await fmt.make_filesystem(guard, f"{loop_4k}p1", "vfat", "IPOD")
    out = await mnt.run_cmd(["fsck.fat", "-n", "-v", f"{loop_4k}p1"])
    assert "4096 bytes per logical sector" in out
    assert "16384 bytes per cluster" in out, out
    assert re.search(r"\b63 hidden sectors", out), out


def test_mount_refuses_system_disk(guard, tmp_path):
    import asyncio

    root = next(
        d for d in scan_block_devices(with_udev=False) if d.name.startswith(("nvme", "mmcblk"))
    )
    for target in [root.path, *(p.path for p in root.partitions())]:
        with pytest.raises(GuardError):
            asyncio.run(mnt.mount(guard, target, "x", tmp_path))
        with pytest.raises(GuardError):
            asyncio.run(fmt.format_disk(guard, target, "vfat", "X"))


def test_mount_options():
    assert "uid=1000" in mnt.mount_options("vfat", 1000, 1000) and "utf8" in mnt.mount_options(
        "vfat", 1, 1
    )
    assert "ro" in mnt.mount_options("ext4", 0, 0, read_only=True)
    assert not any(o.startswith("uid") for o in mnt.mount_options("ext4", 1, 1))


@pytest.mark.parametrize(
    ("label", "fs", "want"),
    [
        ("Мій Flash", "vfat", "FLASH"),
        ("my flash drive!", "vfat", "MY FLASH DR"),
        ("Мій Flash", "exfat", "Мій Flash"),
        ("", "ext4", "SYNCDIR"),
        ("a" * 40, "ntfs", "a" * 32),
    ],
)
def test_clean_label(label, fs, want):
    assert fmt.clean_label(label, fs) == want


def test_partition_path():
    from syncmusic.infra.blockdev.identify import BlockDeviceInfo

    assert fmt.partition_path(BlockDeviceInfo(name="sda", path="/dev/sda")) == "/dev/sda1"
    assert fmt.partition_path(BlockDeviceInfo(name="loop3", path="/dev/loop3")) == "/dev/loop3p1"


def test_mkfs_vfat_geometry():
    plain = fmt.mkfs_command("/dev/sdz1", "vfat", "iPod", fmt.Geometry(512, 2048))
    assert plain == ["mkfs.vfat", "-F", "32", "-n", "IPOD", "/dev/sdz1"]
    big = fmt.mkfs_command("/dev/sdz1", "vfat", "iPod", fmt.Geometry(4096, 504))
    assert big[-7:] == ["-S", "4096", "-s", "4", "-h", "63", "/dev/sdz1"]
    assert fmt.mkfs_command("/dev/sdz1", "exfat", "x", fmt.Geometry(4096, 504))[0] == "mkfs.exfat"

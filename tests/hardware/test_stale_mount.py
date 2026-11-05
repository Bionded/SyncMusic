"""On the real Zorvex flash: unplug without eject (USB port deauthorised) must not leave a stale
mount, and a replug must not stack a second mount on the same path. Read-only on the flash's data."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

import pytest

from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.guard import Guard
from syncmusic.infra.blockdev.identify import scan_block_devices
from tests.conftest import HARDWARE_TOML
from tests.hardware.conftest import hw_value

pytestmark = pytest.mark.hardware

SERIAL = hw_value("flash", "usb_serial")


def _flash_partition() -> str | None:
    for d in scan_block_devices(True):
        if d.serial == SERIAL and d.tran == "usb":
            parts = [c for c in d.children if c.fstype]
            return (parts[0] if parts else d).path
    return None


def _disk(part: str) -> str:
    for d in scan_block_devices(True):
        if part == d.path or any(c.path == part for c in d.children):
            return d.path
    return part


def _usb_port(dev_path: str) -> Path:
    """/sys/bus/usb/devices/<port> of the disk, checked against the allowlisted serial."""
    sys_path = Path("/sys/class/block") / Path(dev_path).name
    p = sys_path.resolve()
    while p != Path("/") and _sysfs_serial(p) != SERIAL:
        p = p.parent
    assert p != Path("/"), "no USB device with the allowlisted flash's serial"
    return p


def _sysfs_serial(p: Path) -> str | None:
    """A sysfs node's `serial`; None where there is none or it cannot be read (some hubs and controllers: ENXIO)."""
    try:
        return (p / "serial").read_text().strip()
    except OSError:
        return None


async def _authorize(port: Path, value: int) -> None:
    await mnt.run_cmd(["sh", "-c", f"echo {value} > {port / 'authorized'}"])


async def _wait(cond, timeout=30.0):
    for _ in range(int(timeout * 4)):
        if await asyncio.to_thread(cond):
            return
        await asyncio.sleep(0.25)
    raise TimeoutError


def _entries(target: Path) -> list[mnt.MountEntry]:
    return [e for e in mnt.mount_table() if e.path == str(target)]


@pytest.mark.parametrize("drop", [True, False], ids=["drop_on_remove", "replug_detaches_stale"])
async def test_unplug_leaves_no_stale_mount(tmp_path, drop):
    part = _flash_partition()
    if part is None:
        pytest.skip("Zorvex flash not connected")
    guard = Guard(HARDWARE_TOML)
    root = tmp_path / "mnt"
    port = _usb_port(part)
    m = await mnt.mount(guard, part, "zorvex", root, read_only=True, registered=[SERIAL])
    try:
        assert m.owned and len(_entries(m.path)) == 1
        await _authorize(port, 0)
        await _wait(lambda: _flash_partition() is None)
        assert len(_entries(m.path)) == 1 and mnt.is_stale(
            _entries(m.path)[0]
        )  # the kernel keeps it
        if drop:
            await mnt.drop_mount(m)
            assert _entries(m.path) == []
        await _authorize(port, 1)
        # the disk node appears before its partition table is probed: wait for the partition itself
        await _wait(lambda: (p := _flash_partition()) is not None and p != _disk(p))
        part2 = _flash_partition()
        assert part2 is not None
        m = await mnt.mount(guard, part2, "zorvex", root, read_only=True, registered=[SERIAL])
        assert m.owned and len(_entries(m.path)) == 1 and not mnt.is_stale(_entries(m.path)[0])
    finally:
        if (port / "authorized").read_text().strip() != "1":
            await _authorize(port, 1)
        while _entries(m.path):
            await mnt.run_cmd(["umount", "-l", str(m.path)])
        await _remove_mountpoint(m.path)


async def test_mount_removed_by_hand_is_not_live(tmp_path):
    """after a manual umount the mountpoint is a bare directory on the system disk;
    is_live must say so (open_device then mounts again), and a re-mount lands on the flash."""
    part = _flash_partition()
    if part is None:
        pytest.skip("Zorvex flash not connected")
    guard = Guard(HARDWARE_TOML)
    root = tmp_path / "mnt"
    m = await mnt.mount(guard, part, "zorvex", root, read_only=True, registered=[SERIAL])
    try:
        assert m.owned and mnt.is_live(m)
        await mnt.run_cmd(["umount", str(m.path)])
        assert m.path.is_dir() and not mnt.is_live(m)
        m = await mnt.mount(guard, part, "zorvex", root, read_only=True, registered=[SERIAL])
        assert mnt.is_live(m)
        assert os.stat(m.path).st_dev != os.stat("/").st_dev  # on the flash, not the NVMe
    finally:
        while _entries(m.path):
            await mnt.run_cmd(["umount", "-l", str(m.path)])
        await _remove_mountpoint(m.path)


async def _remove_mountpoint(path: Path) -> None:
    """Teardown: a raw `umount -l` leaves the root-created mountpoint directory behind (gone already: ignored)."""
    with contextlib.suppress(mnt.BlockdevError):
        await mnt.run_cmd(["rmdir", str(path)])

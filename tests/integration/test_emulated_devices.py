"""The emulated devices themselves (tests/emulated.py): each image carries its USB identity, is recognised
by the right device class, and the guard protects the emulated iPods like the real ones. Real disks are never
touched: their lsblk view is the same before and after."""

from __future__ import annotations

import subprocess

import pytest

from syncmusic.devices.flashdrive import FlashDrive
from syncmusic.devices.iPod.ipod import IPod
from syncmusic.devices.Rockbox.rockbox import Rockbox
from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.guard import GuardError
from syncmusic.infra.blockdev.identify import find_disk_of, scan_block_devices
from tests.emulated import (
    CLASSIC_LAYOUT,
    MINI_LAYOUT,
    attach,
    emulated_guard,
    flash,
    ipod,
    needs_sudo,
    rockbox,
    serial_of,
)

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]


def _real_disks() -> str:
    # no MOUNTPOINTS: the live instance may mount a real device meanwhile; a mount changes no data
    cols = "NAME,SIZE,TYPE,FSTYPE,LABEL,UUID,PARTTYPE"
    out = subprocess.run(["lsblk", "-rn", "-o", cols], capture_output=True, text=True, check=True)
    return "\n".join(line for line in out.stdout.splitlines() if not line.startswith("loop"))


@pytest.fixture(autouse=True)
def real_disks_untouched():
    before = _real_disks()
    yield
    assert _real_disks() == before


def _disk(dev: str):
    return find_disk_of(dev, scan_block_devices(with_udev=False))[0]


@pytest.mark.parametrize("fs", ["vfat", "exfat", "ext4", "ntfs"])
async def test_flash(tmp_path, fs):
    with attach("emulated_flash") as disk:
        part = await flash(disk, fs, "EMU")
        blk = _disk(disk.dev)
        assert (blk.serial, blk.usb_vendor_id, blk.usb_product_id, blk.rm) == (
            serial_of("emulated_flash"),
            "0951",
            "1666",
            True,
        )
        assert [p.path for p in blk.partitions()] == [part]
        m = await mnt.mount(emulated_guard(), part, "f", tmp_path)
        try:
            assert FlashDrive.probe(blk, m.path) is not None
            assert IPod.probe(blk, m.path) is None and Rockbox.probe(blk, m.path) is None
        finally:
            await mnt.unmount(m)


@pytest.mark.parametrize(
    ("kind", "layout"),
    [("emulated_ipod_classic", CLASSIC_LAYOUT), ("emulated_ipod_mini", MINI_LAYOUT)],
)
async def test_stock_ipod(tmp_path, kind, layout):
    with attach(kind, 96, layout=layout) as disk:
        data = await ipod(disk, tmp_path / "build")
        blk = _disk(disk.dev)
        assert blk.usb_vendor_id == "05ac" and blk.serial == disk.serial
        guard = emulated_guard()
        with pytest.raises(GuardError, match="partition table"):
            guard.check(disk.dev, "partition")
        if kind == "emulated_ipod_mini":
            with pytest.raises(GuardError, match="firmware partition"):
                guard.check(disk.part(1), "mkfs")
        guard.check(data, "mkfs")  # the data partition may be formatted
        m = await mnt.mount(guard, data, "i", tmp_path / "mnt")
        try:
            probe = IPod.probe(blk, m.path)
            assert probe is not None
            sysinfo = (m.path / "iPod_Control" / "Device" / "SysInfo").read_text()
            assert f"FirewireGuid: 0x{disk.serial}" in sysinfo
        finally:
            await mnt.unmount(m)


async def test_rockbox(tmp_path):
    with attach("emulated_rockbox", 96, layout=MINI_LAYOUT) as disk:
        data = await rockbox(disk, tmp_path / "build")
        blk = _disk(disk.dev)
        with pytest.raises(GuardError, match="firmware partition"):
            emulated_guard().check(disk.part(1), "mkfs")
        m = await mnt.mount(emulated_guard(), data, "r", tmp_path / "mnt")
        try:
            assert Rockbox.probe(blk, m.path) is not None
            assert (m.path / ".rockbox" / "config.cfg").read_bytes().endswith(b"play log: off\r\n")
        finally:
            await mnt.unmount(m)


async def test_clock_settle_on_an_idle_ipod_ends_after_the_minimum(tmp_path):
    """On an emulated Classic: nothing writes to the image, so the settle (SCSI not available on a loop
    device, judged by the direct partition read alone) ends after SETTLE_MIN_S instead of the old fixed 30 s."""
    from syncmusic.infra.blockdev import scsi

    with attach("emulated_ipod_classic", 96, layout=CLASSIC_LAYOUT) as disk:
        data = await ipod(disk, tmp_path / "build")
        waited = await scsi.wait_settled(emulated_guard(), disk.dev, data)
        assert scsi.SETTLE_MIN_S <= waited < scsi.SETTLE_MIN_S + 3

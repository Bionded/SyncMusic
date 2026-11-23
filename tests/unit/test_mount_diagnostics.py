# SPDX-License-Identifier: GPL-3.0-or-later
"""Diagnostics around I/O errors: kernel messages about the disk, write-through options of a reused mount."""

from __future__ import annotations

import logging
import subprocess

from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.identify import BlockDeviceInfo

DMESG = """[ 10.0] usb 2-1: reset high-speed USB device number 3 using xhci_hcd
[ 10.1] sd 0:0:0:0: [sdq] tag#0 FAILED Result: hostbyte=DID_ERROR
[ 10.2] I/O error, dev sdq, sector 2048 op 0x1:(WRITE)
[ 10.3] FAT-fs (sdq1): unable to read inode block
[ 10.4] FAT-fs (sdr1): unrelated disk
[ 10.5] usb 2-1: USB disconnect, device number 3
"""


def test_kernel_excerpt_keeps_disk_partition_and_usb_lines(monkeypatch):
    def run(*a, **kw):
        return subprocess.CompletedProcess(a, 0, DMESG, "")

    monkeypatch.setattr(mnt.subprocess, "run", run)
    lines = mnt.kernel_excerpt(["/dev/sdq1"])
    assert [ln.split("]", 1)[0] for ln in lines] == ["[ 10.0", "[ 10.1", "[ 10.3", "[ 10.5"]
    assert mnt.kernel_excerpt([]) == []


class Guard:
    def check(self, *a, **kw):
        return None


async def test_reused_mount_with_sync_or_flush_is_warned_about(monkeypatch, tmp_path, caplog):
    entry = mnt.MountEntry(
        "8:1", "/media/x", "/dev/sdq1", "vfat", options=frozenset({"rw", "flush"})
    )
    node = BlockDeviceInfo(name="sdq1", path="/dev/sdq1", fstype="vfat")
    monkeypatch.setattr(mnt, "mounts_of", lambda dev: [entry])
    monkeypatch.setattr(mnt, "_node", lambda p, disks: node)
    monkeypatch.setattr(mnt, "scan_block_devices", lambda *_: [])
    with caplog.at_level(logging.WARNING):
        m = await mnt.mount(Guard(), "/dev/sdq1", "mini", tmp_path)
    assert not m.owned
    assert "uses flush" in caplog.text
    caplog.clear()
    monkeypatch.setattr(
        mnt, "mounts_of", lambda dev: [mnt.MountEntry("8:1", "/media/x", "/dev/sdq1", "vfat")]
    )
    with caplog.at_level(logging.WARNING):
        await mnt.mount(Guard(), "/dev/sdq1", "mini", tmp_path)
    assert "uses" not in caplog.text


def test_mount_table_reads_super_options(monkeypatch, tmp_path):
    info = tmp_path / "mountinfo"
    info.write_text("36 25 8:1 / /media/x rw,relatime shared:5 - vfat /dev/sdq1 rw,flush,utf8\n")
    real = mnt.Path

    monkeypatch.setattr(
        mnt, "Path", lambda p: real(info) if p == "/proc/self/mountinfo" else real(p)
    )
    (e,) = mnt.mount_table()
    assert {"flush", "relatime", "rw"} <= e.options

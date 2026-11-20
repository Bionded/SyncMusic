# SPDX-License-Identifier: GPL-3.0-or-later
"""umount tries again for a moment while the file system is busy (udev / udisks probing a new mount)."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.mount import BlockdevError


@pytest.fixture
def umount(monkeypatch):
    calls: list[list[str]] = []
    busy = {"left": 0}

    async def run_cmd(cmd, **_kw):
        calls.append(list(cmd))
        if cmd[0] == "umount" and busy["left"]:
            busy["left"] -= 1
            raise BlockdevError(f"umount {cmd[1]} failed (32): umount: {cmd[1]}: target is busy.")
        return ""

    monkeypatch.setattr(mnt, "run_cmd", run_cmd)
    monkeypatch.setattr(mnt, "UMOUNT_BUSY_WAIT_S", 0)
    return calls, busy


async def test_a_moment_busy_is_waited_out(umount):
    calls, busy = umount
    busy["left"] = 2
    await mnt._umount_retrying(Path("/mnt/x"))
    assert [c[0] for c in calls] == ["umount"] * 3


async def test_busy_for_good_still_fails(umount):
    calls, busy = umount
    busy["left"] = 99
    with pytest.raises(BlockdevError, match="busy"):
        await mnt._umount_retrying(Path("/mnt/x"))
    assert len(calls) == mnt.UMOUNT_BUSY_TRIES


async def test_unmount_all_finds_fuse_mounts_and_waits_out_busy(umount, monkeypatch):
    """a FUSE mount (ntfs-3g, exfat-fuse) is not in lsblk's mountpoints: the mount table finds it, and a
    moment busy is retried before mkfs or partitioning runs."""
    from syncmusic.infra.blockdev.identify import BlockDeviceInfo

    calls, busy = umount
    part = BlockDeviceInfo(name="sdz1", path="/dev/sdz1", type="part", fstype="ntfs")
    disk = BlockDeviceInfo(name="sdz", path="/dev/sdz", type="disk", children=[part])
    fuse = mnt.MountEntry(dev="0:77", path="/media/x", source="/dev/sdz1", fstype="fuseblk")
    monkeypatch.setattr(mnt, "mounts_of", lambda p, table=None: [fuse] if p == "/dev/sdz1" else [])
    busy["left"] = 1
    await mnt.unmount_all(disk)
    assert calls == [["umount", "/media/x"], ["umount", "/media/x"]]


class _Guard:
    def check(self, *a, **kw):
        return None, None


async def test_repair_refuses_a_fuse_mounted_volume(umount, monkeypatch):
    from syncmusic.infra.blockdev.identify import BlockDeviceInfo

    part = BlockDeviceInfo(name="sdz1", path="/dev/sdz1", type="part", fstype="exfat")
    disk = BlockDeviceInfo(name="sdz", path="/dev/sdz", type="disk", children=[part])
    fuse = mnt.MountEntry(dev="0:77", path="/media/x", source="/dev/sdz1", fstype="fuseblk")
    monkeypatch.setattr(mnt, "scan_block_devices", lambda _: [disk])
    monkeypatch.setattr(mnt, "mounts_of", lambda p, table=None: [fuse])
    with pytest.raises(BlockdevError, match="mounted at /media/x"):
        await mnt.repair(_Guard(), "/dev/sdz1")


async def test_mount_undoes_itself_when_chown_fails(umount, monkeypatch, tmp_path):
    from syncmusic.infra.blockdev.identify import BlockDeviceInfo

    calls, _ = umount
    part = BlockDeviceInfo(name="sdz1", path="/dev/sdz1", type="part", fstype="ext4")
    disk = BlockDeviceInfo(name="sdz", path="/dev/sdz", type="disk", children=[part])
    base = mnt.run_cmd

    async def run_cmd(cmd, **kw):
        if cmd[0] == "chown":
            calls.append(list(cmd))
            raise BlockdevError("chown failed")
        return await base(cmd, **kw)

    async def nothing(*a, **kw):
        return None

    monkeypatch.setattr(mnt, "run_cmd", run_cmd)
    monkeypatch.setattr(mnt, "scan_block_devices", lambda _: [disk])
    monkeypatch.setattr(mnt, "mounts_of", lambda p, table=None: [])
    monkeypatch.setattr(mnt, "mount_table", lambda: [])
    monkeypatch.setattr(mnt, "_detach", nothing)
    monkeypatch.setattr(mnt.os, "geteuid", lambda: 1000)
    with pytest.raises(BlockdevError, match="chown"):
        await mnt.mount(_Guard(), "/dev/sdz1", "dev1", tmp_path)
    target = str(tmp_path / "dev1")
    assert [c[0] for c in calls][-3:] == ["chown", "umount", "rmdir"], calls
    assert calls[-1] == ["rmdir", target]

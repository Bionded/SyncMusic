# SPDX-License-Identifier: GPL-3.0-or-later
"""Unmount a device everywhere: its own mount and those a host automounter made."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.guard import GuardError
from syncmusic.infra.blockdev.identify import BlockDeviceInfo

ROOT = Path("/run/syncmusic-test")


def player(*mountpoints: str) -> BlockDeviceInfo:
    return BlockDeviceInfo(
        name="sdy",
        path="/dev/sdy",
        tran="usb",
        rm=True,
        children=[
            BlockDeviceInfo(name="sdy1", path="/dev/sdy1", type="part"),
            BlockDeviceInfo(
                name="sdy2", path="/dev/sdy2", type="part", mountpoints=list(mountpoints)
            ),
        ],
    )


def test_disk_mounts_lists_both_mountpoints_of_a_partition():
    disk = player(f"{ROOT}/dev1", "/media/someone/PLAYER")
    assert mnt.disk_mounts(disk, ROOT) == [
        mnt.PartitionMount("/dev/sdy2", f"{ROOT}/dev1", True),
        mnt.PartitionMount("/dev/sdy2", "/media/someone/PLAYER", False),
    ]


class FakeGuard:
    def __init__(self, disk: BlockDeviceInfo, refuse: bool = False) -> None:
        self.disk, self.refuse, self.ops = disk, refuse, []

    def check(self, target, operation, registered=None, disks=None):
        self.ops.append(operation)
        if self.refuse:
            raise GuardError(f"{target}: not a USB disk")
        return self.disk, self.disk


@pytest.fixture
def cmds(monkeypatch):
    calls: list[list[str]] = []
    busy: set[str] = set()

    async def run_cmd(cmd, **_kw):
        calls.append(list(cmd))
        if cmd[:1] == ["umount"] and cmd[-1] in busy and "-l" not in cmd:
            raise BlockdevError(f"umount {cmd[-1]} failed (32): target is busy.")
        return ""

    async def flush(_path):
        return None

    monkeypatch.setattr(mnt, "run_cmd", run_cmd)
    monkeypatch.setattr(mnt, "_flush", flush)
    monkeypatch.setattr(mnt, "UMOUNT_BUSY_WAIT_S", 0)
    monkeypatch.setattr(mnt, "mount_holders", lambda path: ["thunar (4242)"])
    return calls, busy


BlockdevError = mnt.BlockdevError


async def test_unmounts_own_and_foreign_mounts(cmds):
    calls, _ = cmds
    guard = FakeGuard(player(f"{ROOT}/dev1", "/media/someone/PLAYER"))
    done = await mnt.unmount_everywhere(guard, "/dev/sdy", mount_root=ROOT)
    assert [m.path for m in done] == [f"{ROOT}/dev1", "/media/someone/PLAYER"]
    assert calls == [["umount", f"{ROOT}/dev1"], ["umount", "/media/someone/PLAYER"]]


async def test_busy_mount_names_the_holder_and_lazy_detaches_it(cmds):
    calls, busy = cmds
    busy.add("/media/someone/PLAYER")
    guard = FakeGuard(player("/media/someone/PLAYER"))
    with pytest.raises(mnt.BusyMountError, match=r"thunar \(4242\)"):
        await mnt.unmount_everywhere(guard, "/dev/sdy")
    calls.clear()
    await mnt.unmount_everywhere(guard, "/dev/sdy", lazy=True)
    assert calls[-1] == ["umount", "-l", "/media/someone/PLAYER"]


async def test_guard_refusal_unmounts_nothing(cmds):
    calls, _ = cmds
    with pytest.raises(GuardError):
        await mnt.unmount_everywhere(FakeGuard(player("/"), refuse=True), "/dev/nvme0n1")
    assert calls == []


def test_mount_holders_finds_open_files(tmp_path):
    proc = tmp_path / "proc"
    (proc / "77" / "fd").mkdir(parents=True)
    (proc / "77" / "comm").write_text("player-app\n")
    os.symlink("/media/x/PLAYER/song.mp3", proc / "77" / "fd" / "3")
    (proc / "88" / "fd").mkdir(parents=True)
    os.symlink("/media/x/PLAYERS/other", proc / "88" / "cwd")
    assert mnt.mount_holders("/media/x/PLAYER", proc) == ["player-app (77)"]

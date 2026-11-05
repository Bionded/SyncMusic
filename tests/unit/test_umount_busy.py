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

"""Stock iPod time sync on the real Classic: the vendor WRITE BUFFER is accepted by the device.

Sends the correct current time while nothing of the iPod is mounted (the iPod then writes its Device files: the
test waits `SETTLE_S` so a following hardware test does not mount it meanwhile). Reading the clock back needs a play on the device (its timestamp in
Play Counts) - a test session cannot press buttons, so the user checks the clock on the screen (note). The
iPod does not rewrite its Device files on every time sync, so their FAT timestamps are no readback either.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from syncmusic.infra.blockdev import scsi
from syncmusic.infra.blockdev.guard import Guard
from tests.conftest import HARDWARE_TOML

pytestmark = pytest.mark.hardware


async def test_classic_accepts_time_sync(classic_disk):
    classic = classic_disk
    guard = Guard(HARDWARE_TOML)
    if classic.all_mountpoints():
        pytest.skip("the Classic is mounted: time sync is refused while mounted")
    await scsi.set_ipod_time(guard, classic.path, dt.datetime.now(dt.UTC))
    await asyncio.sleep(scsi.SETTLE_S)
    data = next(p for p in classic.partitions() if p.fstype == "vfat")
    with pytest.raises(Exception, match="time sync"):
        guard.check(data.path, "time_sync")  # never a partition

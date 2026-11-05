# SPDX-License-Identifier: GPL-3.0-or-later
"""The block-device guard on the real allowlisted players (tests/hardware.toml), read-only: nothing is mounted or
written. A flash drive may be repartitioned; an iPod's partition table never, nor the mkfs of a Rockbox mini's
firmware partition. One test per device."""

from __future__ import annotations

import pytest

from syncmusic.infra.blockdev.guard import Guard, GuardError
from syncmusic.infra.blockdev.identify import scan_block_devices
from tests.conftest import HARDWARE_TOML

pytestmark = pytest.mark.hardware


def test_flash_may_be_repartitioned(flash_disk):
    Guard(HARDWARE_TOML).check(flash_disk.path, "partition", disks=scan_block_devices())


def test_classic_partition_table_refused(classic_disk):
    with pytest.raises(GuardError):
        Guard(HARDWARE_TOML).check(classic_disk.path, "partition", disks=scan_block_devices())


def test_mini_partition_table_and_firmware_refused(mini_disk):
    guard, disks = Guard(HARDWARE_TOML), scan_block_devices()
    with pytest.raises(GuardError):
        guard.check(mini_disk.path, "partition", disks=disks)
    with pytest.raises(GuardError):
        guard.check(mini_disk.partitions()[0].path, "mkfs", disks=disks)

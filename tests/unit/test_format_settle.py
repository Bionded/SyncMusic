# SPDX-License-Identifier: GPL-3.0-or-later
"""After a new file system or label the partition table is not re-read: libparted's partprobe drops the MBR
entries it does not recognise from the kernel, and an iPod's type-0 firmware partition would lose its node."""

from __future__ import annotations

import pytest

from syncmusic.infra.blockdev import format as fmt


@pytest.fixture
def commands(monkeypatch):
    ran: list[list[str]] = []

    async def run_cmd(cmd, **_kw):
        ran.append(list(cmd))
        return ""

    monkeypatch.setattr(fmt, "run_cmd", run_cmd)
    return ran


async def test_no_partprobe_after_a_file_system_or_label(commands):
    await fmt._settle("/dev/sdx")
    assert [c[0] for c in commands] == ["udevadm"]


async def test_partprobe_after_a_new_partition_table(commands):
    await fmt._settle("/dev/sdx", table_written=True)
    assert [c[0] for c in commands] == ["partprobe", "udevadm"]

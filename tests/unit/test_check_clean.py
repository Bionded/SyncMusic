# SPDX-License-Identifier: GPL-3.0-or-later
"""The read-only file system check an eject runs after the unmount, on real (unprivileged) image files."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from syncmusic.infra.blockdev import mount as mnt

pytestmark = pytest.mark.skipif(shutil.which("mkfs.exfat") is None, reason="exfatprogs missing")


@pytest.fixture
def image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(mnt, "privileged", lambda cmd: cmd)  # an image file needs no root
    img = tmp_path / "card.img"
    img.write_bytes(b"")
    subprocess.run(["truncate", "-s", "64M", str(img)], check=True)
    subprocess.run(["mkfs.exfat", str(img)], check=True, capture_output=True)
    return img


async def test_clean_volume_passes(image: Path) -> None:
    await mnt.check_clean(str(image), "exfat")


async def test_damaged_volume_raises(image: Path) -> None:
    with image.open("r+b") as f:  # break the main boot region's checksum sector
        f.seek(11 * 512)
        f.write(b"\xff" * 512)
    with pytest.raises(mnt.DamagedFilesystemError, match="errors right after the sync"):
        await mnt.check_clean(str(image), "exfat")


async def test_other_file_systems_are_not_checked(image: Path) -> None:
    await mnt.check_clean(str(image), "ntfs")  # an exFAT image, but only the named type counts
    await mnt.check_clean(str(image), None)

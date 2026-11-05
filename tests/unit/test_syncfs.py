# SPDX-License-Identifier: GPL-3.0-or-later
"""the app flushes only the device's file system (syncfs(2)), never every file system (sync(2))."""

from pathlib import Path

import pytest

import syncmusic
from syncmusic.infra.blockdev import mount as mnt


def test_syncfs_flushes_the_given_filesystem(tmp_path):
    (tmp_path / "f").write_bytes(b"x")
    mnt.syncfs(tmp_path)


def test_syncfs_missing_path_raises(tmp_path):
    with pytest.raises(OSError):
        mnt.syncfs(tmp_path / "gone")


def test_app_never_calls_global_sync():
    offenders = [
        p.name
        for p in Path(syncmusic.__file__).parent.rglob("*.py")
        if any(s in p.read_text(encoding="utf-8") for s in ("os.sync()", "os.sync)", '["sync"]'))
    ]
    assert offenders == []

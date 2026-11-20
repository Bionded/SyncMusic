"""Mount lost scenario: run on emulated players (tests/integration) and on real ones."""

from __future__ import annotations

import asyncio
import contextlib
import os
import types
from pathlib import Path

import pytest

from syncmusic.core.applier import Applier, DeviceLostError
from syncmusic.core.models import LibraryConfig
from syncmusic.core.planner import plan_sync
from syncmusic.core.prepare import Chain, Preparer
from syncmusic.core.sync import SyncService
from syncmusic.devices.flashdrive import FlashDrive
from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.cache import ProcessingCache
from syncmusic.infra.db import Database
from tests.fakes import FakeLibrary
from tests.unit.test_applier import _cfg


def _entries(target: Path) -> list[mnt.MountEntry]:
    return [e for e in mnt.mount_table() if e.path == str(target)]


def _files(root) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


async def mount_lost_scenario(tmp_path, part, guard, serial):
    """The mount vanishes mid-apply (also on an emulated flash drive, tests/integration/test_emulated_flash.py)."""
    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    for i in range(6):
        (lib_root / f"Трек {i}.mp3").write_bytes(os.urandom(512 * 1024 + i))
    library = FakeLibrary(
        LibraryConfig(id="lib", type="fake", name="L", settings={"root": str(lib_root)}), None
    )
    db = Database(tmp_path / "sd.db")
    await db.open()
    preparer = Preparer(ProcessingCache(db, tmp_path / "cache"), tmp_path / "work")
    m = await mnt.mount(guard, part, "zorvex", tmp_path / "mnt", registered=[serial])
    try:
        assert m.owned and mnt.is_live(m)
        dev = FlashDrive(_cfg(), m.path, None)
        (m.path / ".syncmusic").mkdir(exist_ok=True)
        before = await dev.read_manifest()
        real_put = dev.put_track
        puts = 0

        async def put(s, item):
            nonlocal puts
            ref = await real_put(s, item)
            puts += 1
            if puts == 2:  # the mount is pulled away behind our back
                await mnt.run_cmd(["umount", "-l", str(m.path)])
            return ref

        dev.put_track = put
        snap = await library.snapshot()
        plan = plan_sync(dev.config, {"lib": snap}, before, dev.effective_capabilities(), Chain([]))
        svc = types.SimpleNamespace(_mounts={"dev": m}, _errors={})
        svc._second_mount = lambda cfg, mm: SyncService._second_mount(svc, cfg, mm)  # type: ignore[arg-type]
        check = SyncService._mount_check(svc, dev.config)  # type: ignore[arg-type]
        with pytest.raises(DeviceLostError, match="unmounted during the sync"):
            await Applier(dev, {"lib": library}, Chain([]), preparer, check_device=check).apply(
                plan
            )
        assert puts == 2 and not mnt.is_live(m)
        # the mountpoint is a bare directory on the NVMe now: nothing may have been written there
        assert os.stat(m.path).st_dev == os.stat(tmp_path).st_dev
        assert _files(m.path) == []
        # the flash still has the manifest it had before the sync
        m = await mnt.mount(guard, part, "zorvex", tmp_path / "mnt", registered=[serial])
        after = await FlashDrive(_cfg(), m.path, None).read_manifest()
        assert (after is None and before is None) or (
            after is not None and before is not None and after.items == before.items
        )
    finally:
        while _entries(m.path):
            await mnt.run_cmd(["umount", "-l", str(m.path)])
            await asyncio.sleep(0.1)
        await _remove_mountpoint(m.path)
        await db.close()


async def _remove_mountpoint(path: Path) -> None:
    """Teardown: a raw `umount -l` leaves the root-created mountpoint directory behind (gone already: ignored)."""
    with contextlib.suppress(mnt.BlockdevError):
        await mnt.run_cmd(["rmdir", str(path)])

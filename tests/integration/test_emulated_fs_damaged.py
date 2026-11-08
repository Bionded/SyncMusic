"""A damaged FAT on an emulated flash drive: a broken cluster chain makes reads fail with EIO (the
kernel logs 'FAT-fs: invalid cluster chain'); the device is marked damaged, automatic syncs stop, and Check and
repair (fsck.vfat on the data partition) fixes it and clears the state."""

from __future__ import annotations

import asyncio
import struct
import subprocess

import pytest

from syncmusic.core.sync import RegisterRequest, SyncError
from tests.emulated import attach, flash, needs_sudo, running_app

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]


def _read(part: str, offset: int, size: int) -> bytes:
    return subprocess.run(
        [
            "sudo",
            "-n",
            "dd",
            f"if={part}",
            "bs=1",
            f"skip={offset}",
            f"count={size}",
            "status=none",
        ],
        capture_output=True,
        check=True,
    ).stdout


def _write(part: str, offset: int, data: bytes) -> None:
    subprocess.run(
        [
            "sudo",
            "-n",
            "dd",
            f"of={part}",
            "bs=1",
            f"seek={offset}",
            "conv=notrunc,fsync",
            "status=none",
        ],
        input=data,
        check=True,
    )


def break_cluster_chain(part: str, name83: bytes) -> None:
    """Point the FAT entry of the root-directory file `name83` ('BIG     BIN') at cluster 1, an invalid cluster, in
    every FAT copy (FAT32 only)."""
    boot = _read(part, 0, 512)
    bps, spc, reserved, nfats = (
        struct.unpack_from("<H", boot, 11)[0],
        boot[13],
        struct.unpack_from("<H", boot, 14)[0],
        boot[16],
    )
    fat_size = struct.unpack_from("<I", boot, 36)[0]
    root_cluster = struct.unpack_from("<I", boot, 44)[0]
    data_start = (reserved + nfats * fat_size) * bps
    root = _read(part, data_start + (root_cluster - 2) * spc * bps, spc * bps)
    for i in range(0, len(root), 32):
        if root[i : i + 11] == name83:
            first = (
                struct.unpack_from("<H", root, i + 20)[0] << 16
                | struct.unpack_from("<H", root, i + 26)[0]
            )
            break
    else:
        raise AssertionError(f"{name83!r} not in the root directory")
    for n in range(nfats):
        _write(part, (reserved + n * fat_size) * bps + first * 4, struct.pack("<I", 1))


async def test_damaged_fat_is_marked_and_repaired(tmp_path):
    with attach("emulated_flash", 64) as disk:
        part = await flash(disk, "vfat", "DAMAGED")
        async with running_app(tmp_path, [disk], []) as (app, [blk]):
            cfg = await app.sync.register(
                RegisterRequest(name="Damaged Stick", disk=blk.path, on_connect="manual")
            )
            device = await app.sync.open_device(cfg.id)
            (device.mount / "BIG.BIN").write_bytes(b"x" * 256 * 1024)  # many clusters
            await app.sync._unmount(cfg.id)
            break_cluster_chain(part, b"BIG     BIN")

            device = await app.sync.open_device(cfg.id)
            with pytest.raises(OSError) as e:
                (device.mount / "BIG.BIN").read_bytes()
            assert app.sync.mark_damaged(cfg.id, e.value)
            listed = (await app.sync.list_devices())["registered"][0]
            assert listed["fs_damaged"] and "file system damaged" in listed["error"]
            with pytest.raises(SyncError) as refused:
                await app.sync.update_sync(cfg.id)
            assert refused.value.code == "fs_damaged"

            job = await app.jobs.wait((await app.sync.repair(cfg.id)).id)
            assert job.status == "succeeded", job.error
            assert job.result["fixed"] is True
            listed = (await app.sync.list_devices())["registered"][0]
            assert listed["fs_damaged"] is None and listed["error"] is None
            assert listed["mounted_at"]  # mounted again
            # fsck cut the file at the broken link: it reads without an I/O error now
            device = await app.sync.open_device(cfg.id)
            (device.mount / "BIG.BIN").read_bytes()


def _sudo(*args: str, check: bool = True) -> None:
    subprocess.run(["sudo", "-n", *args], check=check)


def _hold(path) -> subprocess.Popen:
    """A process whose working directory keeps the mount busy."""
    return subprocess.Popen(["sleep", "60"], cwd=path)


async def test_foreign_second_mount_blocks_repair_until_unmounted_everywhere(tmp_path):
    """A host automounter mounts the data partition a second time: Repair refuses with
    `mounted_elsewhere`, Unmount everywhere removes both mounts (naming a holder while one is busy) and Repair
    then runs."""
    with attach("emulated_flash", 64) as disk:
        part = await flash(disk, "vfat", "TWICE")
        async with running_app(tmp_path, [disk], []) as (app, [blk]):
            cfg = await app.sync.register(
                RegisterRequest(name="Twice Stick", disk=blk.path, on_connect="manual")
            )
            await app.sync.open_device(cfg.id)
            foreign = tmp_path / "automount"
            foreign.mkdir()
            _sudo("mount", part, str(foreign))
            try:
                mounts = await app.sync.mounts(cfg.id)
                assert str(foreign) in [m["path"] for m in mounts if not m["ours"]]
                with pytest.raises(SyncError) as refused:
                    await app.sync.repair(cfg.id)
                assert refused.value.code == "mounted_elsewhere"
                assert str(foreign) in str(refused.value)

                holder = _hold(foreign)
                try:
                    with pytest.raises(SyncError) as busy:
                        await app.sync.unmount_everywhere(cfg.id)
                    assert busy.value.code == "mount_busy" and "sleep" in str(busy.value)
                finally:
                    holder.kill()
                    holder.wait()

                done = await app.sync.unmount_everywhere(cfg.id)
                assert str(foreign) in [m["path"] for m in done]
                assert await app.sync.mounts(cfg.id) == []
            finally:
                _sudo("umount", str(foreign), check=False)

            job = await app.jobs.wait((await app.sync.repair(cfg.id)).id)
            assert job.status == "succeeded", job.error


async def test_foreign_mount_is_listed_and_unmounted_with_the_setting(tmp_path, monkeypatch):
    """The device list names another program's mount; with `unmount_foreign` the app removes it after
    its own mount."""
    from syncmusic.core.sync import SyncService

    monkeypatch.setattr(SyncService, "FOREIGN_CHECKS_S", (0.2, 0.5))
    with attach("emulated_flash", 64) as disk:
        part = await flash(disk, "vfat", "AUTOMNT")
        async with running_app(tmp_path, [disk], []) as (app, [blk]):
            cfg = await app.sync.register(
                RegisterRequest(name="Auto Stick", disk=blk.path, on_connect="manual")
            )
            foreign = tmp_path / "automount"
            foreign.mkdir()
            _sudo("mount", part, str(foreign))
            try:
                listed = (await app.sync.list_devices())["registered"][0]
                assert listed["foreign_mounts"] == [str(foreign)]

                cfg.sync.unmount_foreign = True
                await app.sync.update_device(cfg)
                await app.sync.open_device(cfg.id)
                for _ in range(40):
                    listed = (await app.sync.list_devices())["registered"][0]
                    if not listed["foreign_mounts"]:
                        break
                    await asyncio.sleep(0.1)
                assert listed["foreign_mounts"] == [] and listed["mounted_at"]
            finally:
                _sudo("umount", str(foreign), check=False)

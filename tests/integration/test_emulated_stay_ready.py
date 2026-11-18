"""Stay ready and existing mounts on an emulated flash drive: a mount made by another program is used
instead of a second mount (Eject by hand removes it), refused when the device says so, a second mount appearing
while the app holds one stops writes, and a stay-ready device is unmounted when idle and re-mounted on demand."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from syncmusic.core.sync import RegisterRequest, SyncError
from tests.emulated import attach, block_device, flash, needs_sudo, running_app

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]


def _sudo(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sudo", "-n", *args], check=check, capture_output=True, text=True)


def _fsck_clean(part: str) -> None:
    r = _sudo("fsck.vfat", "-n", part, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


async def test_existing_mount_is_used_never_a_second_one(tmp_path):
    with attach("emulated_flash", 64) as disk:
        part = await flash(disk, "vfat", "REUSE")
        async with running_app(tmp_path, [disk], []) as (app, [blk]):
            cfg = await app.sync.register(
                RegisterRequest(name="Reuse Stick", disk=blk.path, on_connect="manual")
            )
            await app.sync._unmount(cfg.id)
            foreign = tmp_path / "automount"
            foreign.mkdir()
            # like a desktop automounter: owned by the user
            _sudo("mount", "-o", f"uid={os.getuid()},gid={os.getgid()}", part, str(foreign))
            try:
                device = await app.sync.open_device(cfg.id)
                assert device.mount == foreign
                assert block_device(disk).all_mountpoints() == [str(foreign)]
                (device.mount / "Song.txt").write_text("written through the other mount")
                assert (foreign / "Song.txt").read_text() == "written through the other mount"
                # Eject by hand from the device page removes the other program's mount too
                await app.sync.eject(cfg.id)
                assert block_device(disk).all_mountpoints() == []
            finally:
                _sudo("umount", str(foreign), check=False)
            _fsck_clean(part)


async def test_existing_mount_refused_when_reuse_is_off(tmp_path):
    with attach("emulated_flash", 64) as disk:
        part = await flash(disk, "vfat", "NOREUSE")
        async with running_app(tmp_path, [disk], []) as (app, [blk]):
            cfg = await app.sync.register(
                RegisterRequest(name="No Reuse", disk=blk.path, on_connect="manual")
            )
            await app.sync._unmount(cfg.id)
            cfg.sync.use_existing_mount = False
            await app.sync.update_device(cfg)
            foreign = tmp_path / "automount"
            foreign.mkdir()
            _sudo("mount", part, str(foreign))
            try:
                with pytest.raises(SyncError) as refused:
                    await app.sync.open_device(cfg.id)
                assert refused.value.code == "foreign_mount"
                assert block_device(disk).all_mountpoints() == [str(foreign)]
            finally:
                _sudo("umount", str(foreign), check=False)


async def test_second_mount_appearing_stops_writes(tmp_path):
    from syncmusic.core.applier import DeviceLostError

    with attach("emulated_flash", 64) as disk:
        part = await flash(disk, "vfat", "LATE")
        async with running_app(tmp_path, [disk], []) as (app, [blk]):
            cfg = await app.sync.register(
                RegisterRequest(name="Late Mount", disk=blk.path, on_connect="manual")
            )
            await app.sync.open_device(cfg.id)
            check = app.sync._mount_check(cfg)
            assert check is not None
            await check()  # only our mount: fine
            late = tmp_path / "late"
            late.mkdir()
            _sudo("mount", part, str(late))
            try:
                with pytest.raises((DeviceLostError, SyncError)):
                    await check()
            finally:
                _sudo("umount", str(late), check=False)
            await check()  # the other mount is gone: writing may go on


async def test_stay_ready_unmounts_when_idle_and_remounts(tmp_path, monkeypatch):
    from syncmusic.core.sync import SyncService

    monkeypatch.setattr(SyncService, "READY_IDLE_S", 0.3)
    with attach("emulated_flash", 64) as disk:
        part = await flash(disk, "vfat", "READY")
        async with running_app(tmp_path, [disk], []) as (app, [blk]):
            cfg = await app.sync.register(
                RegisterRequest(name="Ready Stick", disk=blk.path, on_connect="manual")
            )
            cfg.sync.stay_ready = True
            await app.sync.update_device(cfg)
            device = await app.sync.open_device(cfg.id)
            (device.mount / "a.txt").write_text("before idle")
            for _ in range(50):
                if cfg.id not in app.sync._mounts:
                    break
                await asyncio.sleep(0.1)
            assert not block_device(disk).all_mountpoints()
            assert app.sync.ready_state(cfg.id)
            listed = (await app.sync.list_devices())["registered"][0]
            assert listed.get("ready") is True
            _fsck_clean(part)  # idle and unmounted: safe to unplug

            device = await app.sync.open_device(cfg.id)
            assert (device.mount / "a.txt").read_text() == "before idle"
            assert not app.sync.ready_state(cfg.id)


def _mount_options(mountpoint: str) -> list[str]:
    for line in Path("/proc/self/mounts").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if fields[1] == mountpoint:
            return fields[3].split(",")
    raise AssertionError(f"{mountpoint} is not mounted")


async def test_stay_ready_view_mounts_read_only_and_a_writer_remounts(tmp_path, monkeypatch):
    from syncmusic.core.sync import SyncService

    monkeypatch.setattr(SyncService, "READY_IDLE_S", 0.3)
    with attach("emulated_flash", 64) as disk:
        part = await flash(disk, "vfat", "ROVIEW")
        async with running_app(tmp_path, [disk], []) as (app, [blk]):
            cfg = await app.sync.register(
                RegisterRequest(name="View Stick", disk=blk.path, on_connect="manual")
            )
            cfg.sync.stay_ready = True
            await app.sync.update_device(cfg)
            device = await app.sync.open_device(cfg.id)
            (device.mount / "a.txt").write_text("written before")
            await app.sync._unmount(cfg.id)
            assert not block_device(disk).all_mountpoints()

            view = await app.sync.open_device(cfg.id, read_only=True)
            [mp] = block_device(disk).all_mountpoints()
            assert "ro" in _mount_options(mp)
            assert (view.mount / "a.txt").read_text() == "written before"
            with pytest.raises(OSError):
                (view.mount / "b.txt").write_text("must not land")

            writer = await app.sync.open_device(cfg.id)
            [mp] = block_device(disk).all_mountpoints()
            assert "rw" in _mount_options(mp)
            (writer.mount / "b.txt").write_text("written after")
            await app.sync._unmount(cfg.id)
            _fsck_clean(part)

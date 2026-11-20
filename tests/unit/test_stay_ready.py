# SPDX-License-Identifier: GPL-3.0-or-later
"""Stay ready: an idle disk device is unmounted between jobs, never while a job holds it."""

# ruff: noqa: F811 - fixtures imported from test_api_sync
from __future__ import annotations

import asyncio
from pathlib import Path

from syncmusic.infra.blockdev import mount as mnt
from tests.unit.test_api_sync import _register, env, music  # noqa: F401


async def _ready_device(app, c, tmp_path, monkeypatch, *, owned=True, at_own_path=True):
    await _register(c, tmp_path / "flash", name="Flash")
    cfg = await app.store.device("flash")
    cfg = cfg.model_copy(
        update={"mount_path": None, "sync": cfg.sync.model_copy(update={"stay_ready": True})}
    )
    await app.store.put_device(cfg)
    await app.sync.load()
    path = app.config.mount_root / "flash" if at_own_path else Path("/media/someone/FLASH")
    app.sync._mounts["flash"] = mnt.Mounted(path, "/dev/sdz1", "exfat", owned=owned)
    unmounted: list[tuple[Path, bool]] = []

    async def unmount(m, *, force_reused=False):
        unmounted.append((m.path, force_reused))

    monkeypatch.setattr(mnt, "unmount", unmount)
    monkeypatch.setattr(app.sync, "READY_IDLE_S", 0.05)
    monkeypatch.setattr(app.sync, "READY_BUSY_POLL_S", 0.02)
    return unmounted


async def test_idle_device_is_unmounted_after_the_running_job(env, tmp_path, monkeypatch):
    app, c = env
    unmounted = await _ready_device(app, c, tmp_path, monkeypatch)
    q = app.bus.subscribe()
    release = asyncio.Event()

    async def job(ctx):
        await release.wait()
        return {}

    info = app.jobs.submit("sync", "sync Flash", job, device_id="flash")
    await asyncio.sleep(0)
    app.sync._schedule_ready_unmount("flash")
    await asyncio.sleep(0.2)
    assert unmounted == []  # never under a running job
    assert "flash" in app.sync._mounts
    release.set()
    await app.jobs.wait(info.id)
    await asyncio.wait_for(app.sync._ready_tasks.get("flash") or asyncio.sleep(0), 2)
    assert "flash" not in app.sync._mounts
    assert len(unmounted) == 1
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert any(e.type == "device.unmounted" and e.data.get("ready") for e in events)


async def test_a_new_open_restarts_the_idle_wait(env, tmp_path, monkeypatch):
    app, c = env
    unmounted = await _ready_device(app, c, tmp_path, monkeypatch)
    app.sync._schedule_ready_unmount("flash")
    first = app.sync._ready_tasks["flash"]
    app.sync._schedule_ready_unmount("flash")
    await asyncio.gather(first, return_exceptions=True)
    assert first.cancelled()
    await app.sync._ready_tasks["flash"]
    assert len(unmounted) == 1


async def test_another_programs_mount_is_left_alone(env, tmp_path, monkeypatch):
    app, c = env
    unmounted = await _ready_device(app, c, tmp_path, monkeypatch, owned=False, at_own_path=False)
    app.sync._schedule_ready_unmount("flash")
    await app.sync._ready_tasks["flash"]
    assert unmounted == []
    assert "flash" in app.sync._mounts


async def test_a_leftover_mount_at_our_mountpoint_is_unmounted(env, tmp_path, monkeypatch):
    """a mount an earlier run of the app left behind is reused (not owned) but still ours to unmount"""
    app, c = env
    unmounted = await _ready_device(app, c, tmp_path, monkeypatch, owned=False)
    app.sync._schedule_ready_unmount("flash")
    await app.sync._ready_tasks["flash"]
    assert unmounted == [(app.config.mount_root / "flash", True)]


async def test_a_failed_unmount_keeps_the_mount_and_tries_again(env, tmp_path, monkeypatch):
    app, c = env
    await _ready_device(app, c, tmp_path, monkeypatch)
    calls = 0

    async def busy(m, *, force_reused=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise mnt.BlockdevError("umount failed (16): busy")

    monkeypatch.setattr(mnt, "unmount", busy)
    app.sync._schedule_ready_unmount("flash")
    first = app.sync._ready_tasks["flash"]
    await first
    assert "flash" in app.sync._mounts
    await app.sync._ready_tasks["flash"]
    assert calls == 2 and "flash" not in app.sync._mounts


async def test_without_stay_ready_nothing_is_unmounted(env, tmp_path, monkeypatch):
    app, c = env
    unmounted = await _ready_device(app, c, tmp_path, monkeypatch)
    cfg = await app.store.device("flash")
    await app.store.put_device(
        cfg.model_copy(update={"sync": cfg.sync.model_copy(update={"stay_ready": False})})
    )
    await app.sync.load()
    app.sync._schedule_ready_unmount("flash")
    await app.sync._ready_tasks["flash"]
    assert unmounted == []


# --- an existing mount of the device (host automounter) ---------------------------------------


class _Guard:
    def check(self, *a, **k):
        return None


def _mounted_at(monkeypatch, path):
    node = type("N", (), {"mountpoints": [path], "fstype": "exfat"})()
    monkeypatch.setattr(mnt, "scan_block_devices", lambda *a: [])
    monkeypatch.setattr(mnt, "_node", lambda dev, nodes: node)


async def test_an_existing_mount_is_used_never_mounted_twice(monkeypatch, tmp_path):
    _mounted_at(monkeypatch, "/media/auto/PLAYER")
    m = await mnt.mount(_Guard(), "/dev/sdz1", "flash", tmp_path)
    assert m.path == Path("/media/auto/PLAYER") and not m.owned


async def test_an_existing_mount_is_refused_when_reuse_is_off(monkeypatch, tmp_path):
    import pytest

    _mounted_at(monkeypatch, "/media/auto/PLAYER")
    with pytest.raises(mnt.ForeignMountError, match="Use an existing mount"):
        await mnt.mount(_Guard(), "/dev/sdz1", "flash", tmp_path, reuse=False)
    # our own mountpoint from an earlier run is not another program's
    _mounted_at(monkeypatch, str(tmp_path / "flash"))
    m = await mnt.mount(_Guard(), "/dev/sdz1", "flash", tmp_path, reuse=False)
    assert m.path == tmp_path / "flash"


async def test_a_disk_that_just_appeared_waits_for_an_automounter(env, monkeypatch):
    import time

    app, c = env
    slept: list[float] = []

    async def sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    app.sync._appeared["flash"] = time.monotonic()
    await app.sync._wait_for_automount("flash")
    assert slept and 4 < slept[0] <= 5
    slept.clear()
    app.sync._appeared["flash"] = time.monotonic() - 10
    await app.sync._wait_for_automount("flash")
    await app.sync._wait_for_automount("unknown")
    assert slept == []


async def test_no_auto_eject_of_another_programs_mount(env, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from syncmusic.core.models import SyncReport

    app, c = env
    await _ready_device(app, c, tmp_path, monkeypatch, owned=False, at_own_path=False)
    cfg = await app.store.device("flash")
    cfg = cfg.model_copy(update={"sync": cfg.sync.model_copy(update={"eject_after": "any"})})
    report = SyncReport(id="r", device_id="flash", status="succeeded")
    app.sync._plan_auto_eject(cfg, SimpleNamespace(network=False), report, "job", False)
    assert not report.auto_eject
    assert any("another program" in w for w in report.warnings)


def test_mounts_are_matched_by_major_minor_and_fuse_by_source(tmp_path):
    import os

    dev = "/dev/null"  # a real device node with a stable major:minor
    rdev = os.stat(dev).st_rdev
    majmin = f"{os.major(rdev)}:{os.minor(rdev)}"
    table = [
        mnt.MountEntry(majmin, "/media/auto/PLAYER", dev, "exfat"),
        mnt.MountEntry("0:77", "/media/fuse/PLAYER", dev, "fuseblk", ro=True),
        mnt.MountEntry("0:78", "/media/other", "/dev/zero", "fuseblk"),
        mnt.MountEntry("8:99", "/media/reused-name", dev, "vfat"),  # same name, other disk
    ]
    found = [e.path for e in mnt.mounts_of(dev, table)]
    assert found == ["/media/auto/PLAYER", "/media/fuse/PLAYER"]


async def test_a_read_only_foreign_mount_refuses_writes(monkeypatch, tmp_path):
    import pytest

    node = type("N", (), {"mountpoints": [], "fstype": "exfat"})()
    monkeypatch.setattr(mnt, "scan_block_devices", lambda *a: [])
    monkeypatch.setattr(mnt, "_node", lambda dev, nodes: node)
    ro = mnt.MountEntry("0:77", "/media/fuse/PLAYER", "/dev/sdz1", "fuseblk", ro=True)
    monkeypatch.setattr(mnt, "mounts_of", lambda dev: [ro])
    with pytest.raises(mnt.ForeignMountError, match="read-only"):
        await mnt.mount(_Guard(), "/dev/sdz1", "flash", tmp_path)
    m = await mnt.mount(_Guard(), "/dev/sdz1", "flash", tmp_path, read_only=True)
    assert m.path == Path("/media/fuse/PLAYER") and m.fstype == "exfat" and not m.owned


async def test_a_second_mount_appearing_mid_job_stops_writing(env, tmp_path, monkeypatch):
    import pytest

    from syncmusic.core.applier import DeviceLostError

    app, c = env
    await _ready_device(app, c, tmp_path, monkeypatch)
    cfg = await app.store.device("flash")
    own = app.sync._mounts["flash"]
    monkeypatch.setattr(mnt, "is_live", lambda m: True)
    table = [mnt.MountEntry("8:1", str(own.path), "/dev/sdz1", "exfat")]
    monkeypatch.setattr(mnt, "mounts_of", lambda dev: list(table))
    check = app.sync._mount_check(cfg)
    await check()  # only our own mount: fine
    table.append(mnt.MountEntry("8:1", "/media/auto/FLASH", "/dev/sdz1", "exfat"))
    with pytest.raises(DeviceLostError, match="second time"):
        await check()
    assert "second time" in app.sync._errors["flash"]
    table.pop()
    await check()
    assert "flash" not in app.sync._errors


async def _ro_env(app, monkeypatch, tmp_path, *, stay_ready=True):
    from tests.unit.test_unmountable import _disk_device

    await _disk_device(app, on_connect="manual")
    cfg = await app.store.device("zorvex")
    await app.store.put_device(
        cfg.model_copy(update={"sync": cfg.sync.model_copy(update={"stay_ready": stay_ready})})
    )
    await app.sync.load()
    calls: list[tuple[str, bool]] = []
    unmounted: list[Path] = []

    async def mount(guard, dev_path, device_id, mount_root, *, read_only=False, **kw):
        calls.append(("mount", read_only))
        target = tmp_path / ("ro" if read_only else "rw")
        target.mkdir(exist_ok=True)
        return mnt.Mounted(target, dev_path, "vfat", owned=True)

    async def unmount(m, *, force_reused=False):
        unmounted.append(m.path)

    monkeypatch.setattr(mnt, "mount", mount)
    monkeypatch.setattr(mnt, "unmount", unmount)
    monkeypatch.setattr(mnt, "is_live", lambda m: True)
    monkeypatch.setattr(app.sync, "READY_IDLE_S", 60)
    return calls, unmounted


async def test_a_view_mounts_read_only_and_a_writer_remounts_writable(env, tmp_path, monkeypatch):
    app, _ = env
    calls, unmounted = await _ro_env(app, monkeypatch, tmp_path)
    dev = await app.sync.open_device("zorvex", read_only=True)
    assert calls == [("mount", True)] and dev.mount == tmp_path / "ro"
    await app.sync.open_device("zorvex", read_only=True)  # the read-only mount is reused
    assert calls == [("mount", True)]
    dev = await app.sync.open_device("zorvex")
    assert unmounted == [tmp_path / "ro"]
    assert calls == [("mount", True), ("mount", False)] and dev.mount == tmp_path / "rw"
    await app.sync.open_device("zorvex", read_only=True)  # a writable mount serves views too
    assert len(calls) == 2


async def test_without_stay_ready_views_mount_writable(env, tmp_path, monkeypatch):
    app, _ = env
    calls, _ = await _ro_env(app, monkeypatch, tmp_path, stay_ready=False)
    await app.sync.open_device("zorvex", read_only=True)
    assert calls == [("mount", False)]


async def test_a_busy_read_only_mount_refuses_the_writer(env, tmp_path, monkeypatch):
    from syncmusic.core.sync import SyncError

    app, _ = env
    await _ro_env(app, monkeypatch, tmp_path)
    await app.sync.open_device("zorvex", read_only=True)

    async def busy(m, *, force_reused=False):
        raise mnt.BlockdevError("umount failed (16): busy")

    monkeypatch.setattr(mnt, "unmount", busy)
    try:
        await app.sync.open_device("zorvex")
    except SyncError as e:
        assert e.code == "busy"
    else:
        raise AssertionError("expected busy")
    assert app.sync._ro_mounts["zorvex"] is app.sync._mounts["zorvex"]


async def test_our_read_only_mount_is_replaced_for_a_writer(monkeypatch, tmp_path):
    own = str(tmp_path / "flash")
    _mounted_at(monkeypatch, own)
    monkeypatch.setattr(
        mnt, "mounts_of", lambda dev: [mnt.MountEntry("8:1", own, dev, "exfat", ro=True)]
    )
    done: list[str] = []

    async def flush(p):
        done.append(f"flush {p}")

    async def umount(p):
        done.append(f"umount {p}")

    async def run_cmd(cmd, **kw):
        done.append(" ".join(cmd))
        return ""

    async def detach(*a, **k):
        return None

    monkeypatch.setattr(mnt, "_flush", flush)
    monkeypatch.setattr(mnt, "_umount_retrying", umount)
    monkeypatch.setattr(mnt, "run_cmd", run_cmd)
    monkeypatch.setattr(mnt, "_detach", detach)
    monkeypatch.setattr(mnt, "mount_table", lambda: [])
    # a reader keeps it as it is
    m = await mnt.mount(_Guard(), "/dev/sdz1", "flash", tmp_path, read_only=True)
    assert not m.owned and done == []
    m = await mnt.mount(_Guard(), "/dev/sdz1", "flash", tmp_path, reuse=False)
    assert done[:2] == [f"flush {own}", f"umount {own}"]
    assert any(c.startswith("mount ") and ",rw" in c or "rw," in c for c in done[2:])
    assert m.path == Path(own) and m.owned


def test_mountinfo_views_of_the_same_mount_are_not_second_mounts():
    own = "/srv/app/mnt/player"
    table = [
        mnt.MountEntry("8:1", own, "/dev/sdz1", "vfat", peers=frozenset({"41"})),
        mnt.MountEntry(
            "8:1", "/mnt/root" + own, "/dev/sdz1", "vfat"
        ),  # host root inside a container
        mnt.MountEntry("8:1", "/hostview/player", "/dev/sdz1", "vfat", peers=frozenset({"41"})),
        mnt.MountEntry("8:1", "/media/auto/PLAYER", "/dev/sdz1", "vfat", peers=frozenset({"52"})),
    ]
    views = [e.path for e in table[1:] if mnt.is_view_of(e.path, own, table)]
    assert views == ["/mnt/root" + own, "/hostview/player"]
    assert not mnt.is_view_of("/media/auto/PLAYER", own, table)


async def test_a_host_root_view_of_our_mount_does_not_stop_writing(env, tmp_path, monkeypatch):
    app, c = env
    await _ready_device(app, c, tmp_path, monkeypatch)
    cfg = await app.store.device("flash")
    own = app.sync._mounts["flash"]
    monkeypatch.setattr(mnt, "is_live", lambda m: True)
    table = [
        mnt.MountEntry("8:1", str(own.path), "/dev/sdz1", "exfat"),
        mnt.MountEntry("8:1", "/mnt/root" + str(own.path), "/dev/sdz1", "exfat"),
    ]
    monkeypatch.setattr(mnt, "mounts_of", lambda dev: list(table))
    await app.sync._mount_check(cfg)()
    assert "flash" not in app.sync._errors


async def test_an_unplug_cancels_the_waiting_unmount(env, tmp_path, monkeypatch):
    """the idle wait of a mount that is gone (ejected, unplugged) must not fire on the next connection: it
    would unmount the reconnected device under a plan that is reading it."""
    from types import SimpleNamespace

    app, c = env
    unmounted = await _ready_device(app, c, tmp_path, monkeypatch)
    monkeypatch.setattr(app.sync, "READY_IDLE_S", 0.3)
    app.sync._schedule_ready_unmount("flash")
    task = app.sync._ready_tasks["flash"]

    async def removed(name):
        return None

    monkeypatch.setattr(app.sync.detector, "disk_removed", removed)
    app.sync.detector.connected["sdz"] = SimpleNamespace(device_id="flash")
    await app.sync.on_disk_removed("sdz")
    await asyncio.sleep(0.5)
    assert task.cancelled() and "flash" not in app.sync._ready_tasks
    assert unmounted == []

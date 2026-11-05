"""Stock iPod time sync: payload layout (libgpod ipod-scsi.c), command, service hook."""
# ruff: noqa: F811 - pytest fixtures imported from test_api_sync

from __future__ import annotations

import datetime as dt
import os
import time

import pytest

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.core.models import DeviceConfig
from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev import scsi
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.unit.test_api_sync import _apply, env, music  # noqa: F401


@pytest.fixture
def tz(monkeypatch):
    def set_tz(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield set_tz
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


def test_payload_summer_east_of_utc(tz):
    tz("Europe/Berlin")
    local = dt.datetime(2026, 7, 15, 14, 30, 5)  # CEST, UTC+2, day 196 of the year
    payload = scsi.ipod_time_payload(local.astimezone())
    # year BE, yday-1 BE, tz=2h*4, hour, minute, second, dst, 3 zero bytes (libgpod struct iPodTime)
    assert payload == bytes([0x07, 0xEA, 0x00, 0xC3, 8, 14, 30, 5, 1, 0, 0, 0])


def test_payload_winter_west_of_utc_from_utc_input(tz):
    tz("America/New_York")
    now = dt.datetime(2026, 1, 1, 3, 4, 5, tzinfo=dt.UTC)  # = 2025-12-31 22:04:05 EST (UTC-5)
    payload = scsi.ipod_time_payload(now)
    assert payload == bytes([0x07, 0xE9, 0x01, 0x6C, 0xEC, 22, 4, 5, 0, 0, 0, 0])
    with pytest.raises(ValueError):
        scsi.ipod_time_payload(dt.datetime(2026, 1, 1))


def _settle_via(monkeypatch, sleep):
    """The settle (polls the disk) seen by these tests as the old fixed sleep of SETTLE_S."""

    async def settled(*a, **kw):
        await sleep(scsi.SETTLE_S)
        return scsi.SETTLE_S

    monkeypatch.setattr(scsi, "wait_settled", settled)


async def test_command_and_rejection(monkeypatch):
    calls = []

    disk = BlockDeviceInfo(name="sdb", path="/dev/sdb", type="disk")

    class G:
        def check(self, target, op):
            calls.append(("guard", target, op))
            return disk, disk

    async def run_cmd(cmd, input=None, **kw):
        calls.append((cmd, input))
        if os.environ.get("_REJECT"):
            raise scsi.BlockdevError("sg_raw failed (2): SCSI Status: Check Condition")
        return ""

    monkeypatch.setattr(scsi, "run_cmd", run_cmd)
    now = dt.datetime(2026, 10, 18, 12, 0, tzinfo=dt.UTC)
    await scsi.set_ipod_time(G(), "/dev/sdb", now)  # type: ignore[arg-type]
    assert calls[0] == ("guard", "/dev/sdb", "time_sync")
    cmd, payload = calls[1]
    assert cmd == [
        "sg_raw",
        "-s",
        "12",
        "/dev/sdb",
        "3B",
        "01",
        "00",
        "0C",
        "00",
        "00",
        "00",
        "00",
        "0C",
        "00",
    ]
    assert payload == scsi.ipod_time_payload(now)
    monkeypatch.setenv("_REJECT", "1")
    with pytest.raises(scsi.TimeSyncRejected):
        await scsi.set_ipod_time(G(), "/dev/sdb", now)  # type: ignore[arg-type]
    # never while the host has a file system of the iPod mounted: the iPod writes its FAT
    part = BlockDeviceInfo(name="sdb1", path="/dev/sdb1", type="part", mountpoints=["/run/x"])
    disk.children = [part]
    n = len(calls)
    with pytest.raises(scsi.BlockdevError, match="mounted at /run/x"):
        await scsi.set_ipod_time(G(), "/dev/sdb", now)  # type: ignore[arg-type]
    assert len(calls) == n + 1  # guard only, no command


class _Dev:
    def __init__(self, error: Exception | None = None, block: str | None = "/dev/sdz") -> None:
        self.error, self.block_path, self.calls = error, block, 0

    def effective_capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(supports_time_sync=True)

    async def set_time(self, now: dt.datetime) -> None:
        self.calls += 1
        if self.error:
            raise self.error


async def test_service_sets_time_and_turns_off_rejecting_models(env):
    app, c = env
    cfg = DeviceConfig(id="ipod", type="ipod", name="iPod")
    await app.store.put_device(cfg)
    ok = _Dev()
    assert await app.sync._sync_time(cfg, ok) and ok.calls == 1
    assert not await app.sync._sync_time(cfg, _Dev(block=None))  # directory device
    assert not await app.sync._sync_time(cfg, _Dev(RuntimeError("usb hiccup")))  # logged
    assert (await app.sync.config("ipod")).capability_overrides == {}
    assert not await app.sync._sync_time(cfg, _Dev(scsi.TimeSyncRejected("check condition")))
    assert (await app.sync.config("ipod")).capability_overrides == {"supports_time_sync": False}


def test_ipod_setting_turns_capability_off(tmp_path):
    from syncmusic.devices.iPod.ipod import IPod
    from tests.contracts.harness import ipod_tree

    mount = ipod_tree(tmp_path / "ipod")
    on = IPod(
        DeviceConfig(id="i", type="ipod", name="i", settings={"time_sync": True}), mount, None
    )
    off = IPod(DeviceConfig(id="i", type="ipod", name="i"), mount, None)  # default off
    assert on.effective_capabilities().supports_time_sync
    assert not off.effective_capabilities().supports_time_sync


def test_one_clock_setting_and_old_configs_still_load():
    """the iPod setting `time_sync` is the only clock switch; a stored `sync.time_sync` is ignored."""
    from syncmusic.devices.iPod.ipod import IPodSettings

    old = DeviceConfig.model_validate(
        {"id": "i", "type": "ipod", "name": "i", "sync": {"time_sync": True, "backup_keep": 3}}
    )
    assert old.sync.backup_keep == 3
    assert "time_sync" not in old.sync.model_dump()
    assert "time_sync" in IPodSettings.model_json_schema()["properties"]
    assert IPodSettings().time_sync is False  # default off


async def test_eject_sets_time_after_unmount_then_waits(env, monkeypatch):
    """the clock is set at eject after the host unmounted, then the iPod gets time to write."""
    from syncmusic.core.detection import ConnectedDisk
    from syncmusic.core.models import DeviceIdentity

    app, c = env
    order: list[str] = []
    cfg = DeviceConfig(
        id="ipod",
        type="ipod",
        name="iPod",
        identity=DeviceIdentity(usb_serial="S1"),
        settings={"time_sync": True},
    )
    await app.store.put_device(cfg)
    await app.sync.load()
    app.sync.detector.connected["sdz"] = ConnectedDisk(
        disk=BlockDeviceInfo(name="sdz", path="/dev/sdz", serial="S1"), device_id="ipod"
    )

    async def unmount(device_id):
        order.append("unmount")

    async def set_time(guard, disk_path, now, zone=None):
        order.append(f"time {disk_path}")

    async def sleep(s):
        order.append(f"sleep {s:.0f}")

    async def eject(guard, disk_path, registered=None):
        order.append("eject")

    monkeypatch.setattr(app.sync, "_unmount", unmount)
    monkeypatch.setattr(scsi, "set_ipod_time", set_time)
    monkeypatch.setattr(mnt, "eject", eject)
    monkeypatch.setattr("syncmusic.core.sync.asyncio.sleep", sleep)
    _settle_via(monkeypatch, sleep)
    app.sync.ctx.guard = object()  # type: ignore[assignment]
    await app.sync.eject("ipod")
    assert order == ["unmount", "time /dev/sdz", f"sleep {scsi.SETTLE_S:.0f}", "eject"]
    order.clear()
    await app.store.put_device(cfg.model_copy(update={"settings": {}}))  # default: off
    await app.sync.load()
    await app.sync.eject("ipod")
    assert order == ["unmount", "eject"]


async def test_read_sysinfo_extended_from_vpd_pages(monkeypatch):
    """page 0xC0 lists the pages, each page's payload is data[4:4+data[3]] (libgpod ipod-scsi.c)."""
    xml = b'<?xml version="1.0"?><plist version="1.0"><dict><key>A</key><string>b</string></dict></plist>'
    chunks = [xml[:40], xml[40:]]
    pages = {0xC0: bytes([0, 0xC0, 0, 2, 0xC2, 0xC3])}
    pages |= {0xC2 + i: bytes([0, 0xC2 + i, 0, len(c)]) + c + b"\0\0" for i, c in enumerate(chunks)}
    seen = []

    class G:
        def check(self, target, op):
            seen.append(op)

    async def run_cmd_raw(cmd, **kw):
        return pages[int(cmd[1].split("=")[1], 16)]

    monkeypatch.setattr(scsi, "run_cmd_raw", run_cmd_raw)
    assert await scsi.read_sysinfo_extended(G(), "/dev/sdb") == xml  # type: ignore[arg-type]
    assert seen == ["read"]

    async def failing(cmd, **kw):
        raise scsi.BlockdevError("sg_inq failed")

    monkeypatch.setattr(scsi, "run_cmd_raw", failing)
    assert await scsi.read_sysinfo_extended(G(), "/dev/sdb") is None  # type: ignore[arg-type]


async def test_ipod_session_fetches_missing_sysinfo_extended(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from syncmusic.devices.iPod.ipod import IPod
    from tests.contracts.harness import ipod_tree

    mount = ipod_tree(tmp_path / "ipod")
    target = mount / "iPod_Control" / "Device" / "SysInfoExtended"
    target.unlink(missing_ok=True)
    calls = []

    async def read(guard, disk):
        calls.append(disk)
        return b"<plist>x</plist>"

    monkeypatch.setattr(scsi, "read_sysinfo_extended", read)
    dev = IPod(DeviceConfig(id="i", type="ipod", name="i"), mount, SimpleNamespace(guard=object()))  # type: ignore[arg-type]
    await dev._ensure_sysinfo_extended()  # directory device: no block path, nothing to read
    assert calls == [] and not target.exists()
    dev.block_path = "/dev/sdz"
    await dev._ensure_sysinfo_extended()
    assert target.read_bytes() == b"<plist>x</plist>" and calls == ["/dev/sdz"]
    await dev._ensure_sysinfo_extended()  # present: not read again
    assert calls == ["/dev/sdz"]


async def test_no_mount_while_the_ipod_settles_after_time_sync(env, monkeypatch):
    """After the clock is set the iPod writes its own FAT for up to SETTLE_S. Eject is not a
    device job, so a sync/plan started in that window (UI click, API) must not mount the data partition."""
    from syncmusic.core.detection import ConnectedDisk
    from syncmusic.core.models import DeviceIdentity
    from syncmusic.core.sync import SyncError

    app, c = env
    cfg = DeviceConfig(
        id="ipod",
        type="ipod",
        name="iPod",
        identity=DeviceIdentity(usb_serial="S1"),
        settings={"time_sync": True},
    )
    await app.store.put_device(cfg)
    await app.sync.load()
    app.sync.detector.connected["sdz"] = ConnectedDisk(
        disk=BlockDeviceInfo(name="sdz", path="/dev/sdz", serial="S1"),
        device_id="ipod",
        data_partition="/dev/sdz1",
    )
    mounted: list[str] = []

    async def unmount(device_id):
        pass

    async def set_time(guard, disk_path, now, zone=None):
        pass

    async def mount(guard, dev, device_id, root, registered=None):
        mounted.append(dev)
        raise mnt.UnmountableError("test: no real mount")

    events = app.sync.ctx.bus.subscribe()
    seen: list[bool] = []

    async def sleep(s):
        # another request arrives while the iPod settles
        for call in (app.sync.open_device("ipod"), app.sync.plan("ipod"), app.sync.eject("ipod")):
            with pytest.raises(SyncError) as exc:
                await call
            assert exc.value.code == "busy"
        assert app.sync.ctx.jobs.device_busy("ipod")
        seen.append((await app.sync.list_devices())["registered"][0]["ejecting"])

    async def eject(guard, disk_path, registered=None):
        pass

    monkeypatch.setattr(app.sync, "_unmount", unmount)
    monkeypatch.setattr(scsi, "set_ipod_time", set_time)
    monkeypatch.setattr(mnt, "mount", mount)
    monkeypatch.setattr(mnt, "eject", eject)
    monkeypatch.setattr("syncmusic.core.sync.asyncio.sleep", sleep)
    _settle_via(monkeypatch, sleep)
    app.sync.ctx.guard = object()  # type: ignore[assignment]
    await app.sync.eject("ipod")
    assert mounted == [] and seen == [True]
    types = [events.get_nowait().type for _ in range(events.qsize())]
    assert "device.ejecting" in types and types[-1] == "device.ejected"
    assert not app.sync.ctx.jobs.device_busy("ipod")
    assert not (await app.sync.list_devices())["registered"][0]["ejecting"]


# --- on by default, set on connect and at a sync, never while mounted -----------------------------------


async def test_new_devices_get_time_sync_on(env, tmp_path):
    from tests.contracts.harness import ipod_tree

    app, c = env
    for name, settings, expected in (("Pod A", {}, True), ("Pod B", {"time_sync": False}, False)):
        mount = ipod_tree(tmp_path / name.replace(" ", ""))
        r = await c.post(
            "/api/v1/devices/register",
            json={"name": name, "mount_path": str(mount), "type": "ipod", "settings": settings},
        )
        assert r.status_code == 201, r.text
        assert r.json()["settings"]["time_sync"] is expected
    # a flash drive has no clock setting
    (tmp_path / "flash").mkdir()
    r = await c.post(
        "/api/v1/devices/register",
        json={"name": "F", "mount_path": str(tmp_path / "flash"), "type": "flashdrive"},
    )
    assert "time_sync" not in r.json()["settings"]


async def test_clock_on_connect_before_mount_at_sync_and_throttled(env, tmp_path, monkeypatch):
    import asyncio

    from syncmusic.core import sync as sync_mod
    from syncmusic.core.detection import ConnectedDisk
    from syncmusic.core.models import DeviceIdentity
    from tests.contracts.harness import ipod_tree
    from tests.unit.test_api_sync import ALL

    app, c = env
    order: list[str] = []
    target = ipod_tree(tmp_path / "mnt")
    cfg = DeviceConfig(
        id="pod",
        type="ipod",
        name="Pod",
        identity=DeviceIdentity(usb_serial="S2", manifest_device_id="u2"),
        library_ids=["music"],
        rule_set=ALL,
        on_connect="manual",
        settings={"time_sync": True},
    )
    await app.store.put_device(cfg)
    await app.sync.load()
    cd = ConnectedDisk(
        disk=BlockDeviceInfo(name="sdy", path="/dev/sdy", serial="S2"),
        device_id="pod",
        data_partition="/dev/sdy2",
    )
    app.sync.detector.connected["sdy"] = cd
    real_sleep = asyncio.sleep

    async def mount(guard, dev_path, device_id, mount_root, **kw):
        order.append("mount")
        return mnt.Mounted(target, dev_path, "vfat", owned=True)

    async def unmount(m):
        order.append("unmount")

    async def set_time(guard, disk_path, now, zone=None):
        order.append("time WHILE MOUNTED" if "pod" in app.sync._mounts else "time")

    async def sleep(s):
        if s == scsi.SETTLE_S:
            order.append("settle")
        await real_sleep(0)

    async def eject(guard, disk_path, registered=None):
        order.append("eject")

    monkeypatch.setattr(mnt, "mount", mount)
    monkeypatch.setattr(mnt, "unmount", unmount)
    monkeypatch.setattr(mnt, "is_live", lambda m: True)
    monkeypatch.setattr(mnt, "eject", eject)

    async def no_sysinfo(guard, disk_path):
        return None  # the virtual iPod has its SysInfo; nothing is sent to a disk

    monkeypatch.setattr(scsi, "set_ipod_time", set_time)
    monkeypatch.setattr(scsi, "read_sysinfo_extended", no_sysinfo)
    monkeypatch.setattr("syncmusic.core.sync.asyncio.sleep", sleep)
    _settle_via(monkeypatch, sleep)
    app.sync.ctx.guard = object()  # type: ignore[assignment]

    # connect: the clock first, the mount waits for it
    events = app.sync.ctx.bus.subscribe()
    await app.sync.on_disk_added(cd)
    assert (await c.get("/api/v1/devices")).json()["registered"][0]["setting_clock"] in (
        True,
        False,
    )
    await app.sync.open_device("pod")
    assert order == ["time", "settle", "mount"]
    # the UI learns when the settle is over and when the disk got mounted
    await real_sleep(0)  # the done callback runs on the next loop pass
    seen = [events.get_nowait() for _ in range(events.qsize())]
    kinds = [
        e.type
        for e in seen
        if e.type in ("device.clock_set", "device.clock_done", "device.mounted")
    ]
    assert kinds == ["device.clock_set", "device.clock_done", "device.mounted"], kinds
    assert next(e for e in seen if e.type == "device.clock_done").data == {
        "device_id": "pod",
        "ok": True,
    }
    assert not (await c.get("/api/v1/devices")).json()["registered"][0]["setting_clock"]
    # a sync right after: no second clock command
    order.clear()
    plan = (await c.post("/api/v1/devices/pod/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    assert "time" not in " ".join(order)
    # 31 minutes later: unmount, clock, settle, mount again - before anything is written
    order.clear()
    app.sync._clock_set["pod"] -= sync_mod.CLOCK_FRESH_S + 60
    plan = (await c.post("/api/v1/devices/pod/plan", json={})).json()
    await _apply(app, c, plan["id"])
    assert order[:4] == ["unmount", "time", "settle", "mount"], order
    # eject right after: no clock command (set less than 30 min ago)
    order.clear()
    await app.sync.eject("pod")
    assert order == ["unmount", "eject"]
    assert not any("WHILE MOUNTED" in o for o in order)
    # unplugged: forgotten; time sync off: nothing on connect
    await app.sync.on_disk_removed("sdy")
    assert "pod" not in app.sync._clock_set
    order.clear()
    await app.store.put_device(cfg.model_copy(update={"settings": {}}))  # stored before off
    await app.sync.load()
    app.sync.detector.connected["sdy"] = cd
    await app.sync.on_disk_added(cd)
    await app.sync.open_device("pod")
    assert order == ["mount"]
    app.sync._mounts.clear()  # the fake mount: nothing to unmount at teardown

    # a failing clock command still ends 'Setting the clock…' with an event
    async def broken_time(guard, disk_path, now, zone=None):
        raise OSError("no answer")

    monkeypatch.setattr(scsi, "set_ipod_time", broken_time)
    await app.sync.on_disk_removed("sdy")
    await app.store.put_device(cfg)
    await app.sync.load()
    app.sync.detector.connected["sdy"] = cd
    while not events.empty():
        events.get_nowait()
    await app.sync.on_disk_added(cd)
    await app.sync._clock_tasks["pod"]
    await real_sleep(0)
    done = [
        e
        for e in (events.get_nowait() for _ in range(events.qsize()))
        if e.type == "device.clock_done"
    ]
    assert [e.data for e in done] == [{"device_id": "pod", "ok": False}]
    app.sync.detector.connected.pop("sdy")


async def test_no_mount_while_the_ipod_settles_after_the_sync_clock(env, tmp_path, monkeypatch):
    """At a sync: `_clock_before_sync` unmounts, sets the clock and sleeps SETTLE_S while the iPod
    writes its own FAT. A request that arrives meanwhile (Player contents, the sidebar's used space, a thumbnail)
    goes through open_device and must wait for the settle (or be refused), never mount the data partition."""
    import asyncio

    from syncmusic.core import sync as sync_mod
    from syncmusic.core.detection import ConnectedDisk
    from syncmusic.core.models import DeviceIdentity
    from tests.contracts.harness import ipod_tree
    from tests.unit.test_api_sync import ALL

    app, c = env
    target = ipod_tree(tmp_path / "mnt")
    cfg = DeviceConfig(
        id="pod",
        type="ipod",
        name="Pod",
        identity=DeviceIdentity(usb_serial="S3", manifest_device_id="u3"),
        library_ids=["music"],
        rule_set=ALL,
        on_connect="manual",
        settings={"time_sync": True},
    )
    await app.store.put_device(cfg)
    await app.sync.load()
    app.sync.detector.connected["sdx"] = ConnectedDisk(
        disk=BlockDeviceInfo(name="sdx", path="/dev/sdx", serial="S3"),
        device_id="pod",
        data_partition="/dev/sdx1",
    )
    real_sleep = asyncio.sleep
    settling = False
    order: list[str] = []
    others: list[asyncio.Task[object]] = []
    seen_setting: list[bool] = []

    async def mount(guard, dev_path, device_id, mount_root, **kw):
        order.append("mount DURING SETTLE" if settling else "mount")
        return mnt.Mounted(target, dev_path, "vfat", owned=True)

    async def unmount(m):
        order.append("unmount")

    async def set_time(guard, disk_path, now, zone=None):
        order.append("time")

    async def sleep(s):
        nonlocal settling
        if s == scsi.SETTLE_S:
            settling = True
            seen_setting.append(app.sync._setting_clock("pod"))  # GET /devices shows setting_clock
            # the user opens Player contents / the sidebar asks for the used space while the iPod settles
            others.append(asyncio.create_task(app.sync.space("pod")))
            await real_sleep(0.5)  # long enough for the request's thread hops (config, is_dir)
            settling = False
            return
        await real_sleep(0)

    async def no_sysinfo(guard, disk_path):
        return None

    monkeypatch.setattr(mnt, "mount", mount)
    monkeypatch.setattr(mnt, "unmount", unmount)
    monkeypatch.setattr(mnt, "is_live", lambda m: True)
    monkeypatch.setattr(scsi, "set_ipod_time", set_time)
    monkeypatch.setattr(scsi, "read_sysinfo_extended", no_sysinfo)
    monkeypatch.setattr("syncmusic.core.sync.asyncio.sleep", sleep)
    _settle_via(monkeypatch, sleep)
    app.sync.ctx.guard = object()  # type: ignore[assignment]

    await app.sync.open_device("pod")  # mounted, as after a connect long ago
    app.sync._clock_set["pod"] = (
        time.monotonic() - sync_mod.CLOCK_FRESH_S - 60
    )  # the clock is due at this sync
    order.clear()
    plan = (await c.post("/api/v1/devices/pod/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    await asyncio.gather(*others, return_exceptions=True)
    app.sync._mounts.clear()  # the fake mount: nothing to unmount at teardown
    assert others, "the settle never ran"
    assert "mount DURING SETTLE" not in order, order
    assert seen_setting == [True]
    assert order[:2] == ["unmount", "time"] and "mount" in order[2:], (
        order
    )  # the apply mounts again afterwards
    assert all(not t.exception() for t in others)  # the request was served after the settle


async def test_requests_waiting_for_the_clock_settle_mount_once(env, tmp_path, monkeypatch):
    """When the clock settle ends, every request that waited
    in open_device (the sync's apply, Player contents, the sidebar's used space) mounts the data partition at the same
    time. On the Classic that stacked three mounts of /dev/sda1 on one mountpoint; the later unmount removed only
    one, so the iPod stayed mounted. Expected: one mount, shared by every waiter."""
    import asyncio

    from syncmusic.core.detection import ConnectedDisk
    from syncmusic.core.models import DeviceIdentity
    from tests.contracts.harness import ipod_tree
    from tests.unit.test_api_sync import ALL

    app, c = env
    target = ipod_tree(tmp_path / "mnt")
    cfg = DeviceConfig(
        id="pod",
        type="ipod",
        name="Pod",
        identity=DeviceIdentity(usb_serial="S4", manifest_device_id="u4"),
        library_ids=["music"],
        rule_set=ALL,
        on_connect="manual",
        settings={"time_sync": True},
    )
    await app.store.put_device(cfg)
    await app.sync.load()
    app.sync.detector.connected["sdx"] = ConnectedDisk(
        disk=BlockDeviceInfo(name="sdx", path="/dev/sdx", serial="S4"),
        device_id="pod",
        data_partition="/dev/sdx1",
    )
    mounts: list[str] = []

    async def mount(guard, dev_path, device_id, mount_root, **kw):
        mounts.append(dev_path)
        await asyncio.sleep(0.05)  # the real mount hops to threads (lsblk, mount table, sudo mount)
        return mnt.Mounted(target, dev_path, "vfat", owned=True)

    monkeypatch.setattr(mnt, "mount", mount)
    monkeypatch.setattr(mnt, "is_live", lambda m: True)
    app.sync.ctx.guard = object()  # type: ignore[assignment]

    settle = asyncio.Event()

    async def clock() -> bool:
        await settle.wait()
        return True

    app.sync._clock_tasks["pod"] = asyncio.create_task(clock())
    waiters = [asyncio.create_task(app.sync.open_device("pod")) for _ in range(3)]
    await asyncio.sleep(0.01)
    assert mounts == []  # nobody mounts during the settle
    settle.set()
    devs = await asyncio.gather(*waiters)
    app.sync._mounts.clear()  # the fake mount: nothing to unmount at teardown
    assert all(d.mount == target for d in devs)
    assert mounts == ["/dev/sdx1"], f"{len(mounts)} mounts of one partition: {mounts}"


async def test_concurrent_opens_without_a_clock_mount_once(env, tmp_path, monkeypatch):
    """two requests opening an unmounted device at the same time (no clock involved) share one mount."""
    import asyncio

    from syncmusic.core.detection import ConnectedDisk
    from syncmusic.core.models import DeviceIdentity
    from tests.contracts.harness import ipod_tree
    from tests.unit.test_api_sync import ALL

    app, c = env
    target = ipod_tree(tmp_path / "mnt")
    cfg = DeviceConfig(
        id="pod",
        type="ipod",
        name="Pod",
        identity=DeviceIdentity(usb_serial="S4", manifest_device_id="u4"),
        library_ids=["music"],
        rule_set=ALL,
        on_connect="manual",
    )
    await app.store.put_device(cfg)
    await app.sync.load()
    app.sync.detector.connected["sdx"] = ConnectedDisk(
        disk=BlockDeviceInfo(name="sdx", path="/dev/sdx", serial="S4"),
        device_id="pod",
        data_partition="/dev/sdx1",
    )
    mounts: list[str] = []

    async def mount(guard, dev_path, device_id, mount_root, **kw):
        mounts.append(dev_path)
        await asyncio.sleep(0.05)
        return mnt.Mounted(target, dev_path, "vfat", owned=True)

    monkeypatch.setattr(mnt, "mount", mount)
    monkeypatch.setattr(mnt, "is_live", lambda m: True)
    app.sync.ctx.guard = object()  # type: ignore[assignment]

    devs = await asyncio.gather(app.sync.open_device("pod"), app.sync.open_device("pod"))
    app.sync._mounts.clear()  # the fake mount: nothing to unmount at teardown
    assert all(d.mount == target for d in devs)
    assert mounts == ["/dev/sdx1"]


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


async def _settle_with(monkeypatch, readings):
    clock, seen = _Clock(), []

    async def fingerprint(guard, disk, part):
        seen.append(clock.t)
        return readings(clock.t)

    monkeypatch.setattr(scsi, "_fingerprint", fingerprint)
    waited = await scsi.wait_settled(
        object(), "/dev/sdz", "/dev/sdz2", sleep=clock.sleep, clock=clock
    )
    return waited, seen


async def test_settle_ends_once_the_disk_is_stable(monkeypatch):
    """the iPod writes for ~2 s after the clock is set, then its state stays the same - go on at ~4 s, not
    after the old fixed 30 s."""
    waited, _ = await _settle_with(monkeypatch, lambda t: f"w{t}" if t < 2 else "done")
    assert waited == 4.0  # readings at 2, 3, 4 s are the same


async def test_settle_waits_at_least_the_minimum_and_at_most_the_cap(monkeypatch):
    waited, _ = await _settle_with(monkeypatch, lambda t: "same")
    assert waited == scsi.SETTLE_MIN_S  # stable from the start: still the minimum
    waited, seen = await _settle_with(monkeypatch, lambda t: f"busy {t}")
    assert waited == scsi.SETTLE_S and len(seen) == 30  # never stable: the old 30 s, no longer
    waited, _ = await _settle_with(monkeypatch, lambda t: None if t < 10 else "ok")
    assert waited == 12.0  # "not ready" never counts as stable


async def test_settle_reading_uses_test_unit_ready_and_a_direct_read(monkeypatch):
    cmds = []

    async def run(cmd, **kw):
        cmds.append(cmd[0])
        if cmd[0] == "sg_turs":
            raise mnt.BlockdevError("sg_turs failed (2): device not ready")
        return ""

    async def run_raw(cmd, **kw):
        cmds.append(cmd[0])
        assert "iflag=direct" in cmd  # past the host's page cache: the iPod writes behind its back
        return b"fat"

    class Guard:
        def check(self, target, op, *a, **kw):
            assert op == "read"

    monkeypatch.setattr(scsi, "run_cmd", run)
    monkeypatch.setattr(scsi, "run_cmd_raw", run_raw)
    assert await scsi._fingerprint(Guard(), "/dev/sdz", "/dev/sdz2") is None
    monkeypatch.setattr(scsi, "run_cmd", lambda cmd, **kw: _fail(cmd))  # a loop image: no SCSI
    assert await scsi._fingerprint(Guard(), "/dev/sdz", "/dev/sdz2") is not None
    assert cmds == ["sg_turs", "dd"]


async def _fail(cmd):
    raise mnt.BlockdevError(f"{cmd[0]} failed (72): do_scsi_pt: Invalid argument")

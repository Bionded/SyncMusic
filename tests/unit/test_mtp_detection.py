"""MTP players detected on USB like disks: udev scan, matching, register from the found list,
connected only while plugged in, eject, and the libmtp backend opening only the wanted device."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.core.detection import Detector, probe_disk
from syncmusic.core.models import DeviceConfig, DeviceIdentity
from syncmusic.devices.Android._libmtp import LibMtpBackend
from syncmusic.devices.Android.mtp import AndroidMtp, AndroidMtpSettings, MtpUnavailable
from syncmusic.facade import AppFacade
from syncmusic.infra.blockdev.mtp import mtp_info, scan_mtp_devices, usb_addresses
from syncmusic.infra.blockdev.watcher import BlockWatcher
from syncmusic.infra.events import EventBus
from tests.fake_libmtp import FakeLibMtp, FakePlayer
from tests.fake_mtp import FakeMtpBackend

SERIAL = "MTPTEST0000001"  # allowlisted by the `env` fixture
PROPS = {
    "DEVTYPE": "usb_device",
    "ID_MTP_DEVICE": "1",
    "ID_MODEL": "Unisoc_Phone",
    "ID_VENDOR": "Unisoc",
    "ID_SERIAL_SHORT": SERIAL,
    "ID_VENDOR_ID": "1782",
    "ID_MODEL_ID": "4003",
    "BUSNUM": "001",
    "DEVNUM": "003",
}


def test_udev_entry_becomes_a_pseudo_disk():
    (d,) = scan_mtp_devices([("1-1", PROPS)])
    assert (d.name, d.type, d.tran, d.serial) == ("mtp-1-1", "mtp", "usb", SERIAL)
    assert (d.vendor, d.model, d.usb_vendor_id, d.usb_product_id) == (
        "Unisoc",
        "Unisoc Phone",
        "1782",
        "4003",
    )
    assert not d.children and d.path == "/sys/bus/usb/devices/1-1"
    assert usb_addresses([("1-1", PROPS)]) == {(1, 3): SERIAL}


async def test_watcher_reports_mtp_players():
    events = []

    async def added(d):
        events.append(("added", d.name))

    async def removed(name):
        events.append(("removed", name))

    found = [mtp_info("1-1", PROPS)]
    w = BlockWatcher(added, removed, scan=lambda: list(found), debounce_s=0.01)
    await w.start(use_udev=False)
    found.clear()
    w.handle_uevent("remove", "mtp-1-1")
    await w.settle()
    assert events == [("added", "mtp-1-1"), ("removed", "mtp-1-1")]


async def test_only_android_claims_it_and_it_matches_by_serial_without_probe_mount():
    d = mtp_info("1-1", PROPS)
    (p,) = probe_disk(d)
    assert p.type_name == "android" and p.identity.usb_serial == SERIAL
    assert p.suggested_name == "Unisoc Unisoc Phone"

    async def never(_):
        raise AssertionError("an MTP player is never probe-mounted")

    cfg = DeviceConfig(id="g", type="android", name="G", identity=DeviceIdentity(usb_serial=SERIAL))
    det = Detector(EventBus(), lambda: [cfg], manifest_probe=never)
    cd = await det.disk_added(d)
    assert (cd.device_id, cd.matched_by, cd.data_partition) == ("g", "usb_serial", None)


def _player(tmp_path, serial, address):
    (tmp_path / serial).mkdir()
    return FakePlayer(serial, {"SD card": tmp_path / serial}, address=address)


def test_backend_opens_only_the_device_with_the_udev_serial(tmp_path):
    # MTP reports another serial than udev: the bus address decides, the other phone is never opened
    other, ours = _player(tmp_path, "PHONE", (1, 2)), _player(tmp_path, "EEF9", (1, 3))
    b = LibMtpBackend(SERIAL, FakeLibMtp(other, ours), usb={(1, 2): "PIXEL", (1, 3): SERIAL})
    assert ours.open and not other.calls and not other.open
    b.close()


def test_backend_by_mtp_serial_asks_only_allowlisted_devices(tmp_path):
    other, ours = _player(tmp_path, "PHONE", (1, 2)), _player(tmp_path, "EEF9", (1, 3))
    lib = FakeLibMtp(other, ours)
    opened = []
    real_open = lib.open
    lib.open = lambda raw: opened.append(raw.serial) or real_open(raw)  # type: ignore[method-assign]
    LibMtpBackend("EEF9", lib, usb={(1, 2): "PIXEL", (1, 3): SERIAL}, allowed=frozenset({SERIAL}))
    assert opened == ["EEF9"]
    with pytest.raises(MtpUnavailable, match="no MTP player with serial"):
        LibMtpBackend("NOPE", FakeLibMtp(other), usb={(1, 2): "PIXEL"}, allowed=frozenset({SERIAL}))
    assert not other.open


@pytest.fixture
def config(config, tmp_path):
    """The shared test config with an allowlist that holds the made-up player of these tests."""
    allowlist = tmp_path / "hardware.toml"
    allowlist.write_text(
        f'[android_test]\nusb_serial = "{SERIAL}"\nusb_vendor_id = "1782"\nusb_product_id = "4003"\n'
    )
    config.device_allowlist = allowlist
    return config


@pytest.fixture
async def env(config, tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    backend = FakeMtpBackend(storage)
    monkeypatch.setattr(AndroidMtp, "backend_factory", staticmethod(lambda s, *_: backend))
    app = Application(config)
    await app.start()
    api = create_api(AppFacade(app), manage_lifecycle=False)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
        yield app, c
    await app.stop()


async def test_register_from_the_found_list_connect_eject_unplug(env):
    app, c = env
    d = mtp_info("1-1", PROPS)
    await app._disk_added(d)
    r = await c.post(
        "/api/v1/devices/register",
        json={"name": "Handheld", "disk": d.path, "on_connect": "manual"},  # type from the probe
    )
    assert r.status_code == 201, r.text
    cfg = r.json()
    assert cfg["settings"]["usb_serial"] == SERIAL and cfg["identity"]["usb_serial"] == SERIAL
    stored = await app.store.device(cfg["id"])
    assert app.sync._connected(stored)
    assert app.sync.detector.by_device(cfg["id"]) is not None
    await app.sync.eject(cfg["id"])  # ends nothing but the plans: allowed, unlike a server
    await app.sync.on_disk_removed(d.name)
    assert not app.sync._connected(stored)
    r = await c.post(f"/api/v1/devices/{cfg['id']}/plan", json={})
    assert r.status_code == 409 and "not connected" in r.text, r.text


async def test_device_types_flag_android_as_beta_usb_mtp(env):
    _app, c = env
    types = {x["type"]: x for x in (await c.get("/api/v1/device-types")).json()}
    assert types["android"]["beta"] and types["android"]["usb_mtp"]
    assert not types["flashdrive"]["beta"] and not types["flashdrive"]["usb_mtp"]
    assert "hiby_compas" not in types


async def test_added_by_hand_is_recognised_when_plugged_in(env):
    app, c = env
    r = await c.post(
        "/api/v1/devices/register",
        json={"name": "Handheld", "type": "android", "settings": {"usb_serial": SERIAL}},
    )
    assert r.status_code == 201, r.text
    assert r.json()["identity"]["usb_serial"] == SERIAL
    assert r.json()["update_every_min"] != 360  # plugged in: no server polling interval
    cd = await app.sync.detector.disk_added(mtp_info("1-1", PROPS))
    assert cd.device_id == r.json()["id"]


async def test_jobs_share_one_mtp_session_released_on_eject_and_unplug(
    config, tmp_path, monkeypatch
):
    """Real-device run (691.4): libmtp allows one session per player, so a plan's open session made the sync job
    "busy". Every open_device call gets the same instance; eject and unplug close it."""
    storage = tmp_path / "storage"
    storage.mkdir()
    opened: list[FakeMtpBackend] = []

    def factory(s, *_):
        opened.append(FakeMtpBackend(storage))
        return opened[-1]

    monkeypatch.setattr(AndroidMtp, "backend_factory", staticmethod(factory))
    app = Application(config)
    await app.start()
    try:
        d = mtp_info("1-1", PROPS)
        await app._disk_added(d)
        api = create_api(AppFacade(app), manage_lifecycle=False)
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
            r = await c.post(
                "/api/v1/devices/register",
                json={"name": "Handheld", "disk": d.path, "on_connect": "manual"},
            )
            assert r.status_code == 201, r.text
            cfg = r.json()
        first = await app.sync.open_device(cfg["id"])
        await first.check()
        assert await app.sync.open_device(cfg["id"]) is first
        await first.check()
        assert len([b for b in opened if not b.closed]) == 1
        await app.sync.eject(cfg["id"])
        assert all(b.closed for b in opened)
        await app.sync.on_disk_removed(d.name)
        await app._disk_added(d)
        again = await app.sync.open_device(cfg["id"])
        assert again is not first
        await again.check()
        await app.sync.on_disk_removed(d.name)
        assert all(b.closed for b in opened)
    finally:
        await app.stop()


async def test_wipe_job_empties_only_our_folder(env, tmp_path):
    """Device settings' Wipe for an Android player (691.4): only the base folder's files go."""
    app, c = env
    storage = tmp_path / "storage"
    (storage / "DCIM").mkdir()
    (storage / "DCIM" / "photo.jpg").write_bytes(b"x")
    d = mtp_info("1-1", PROPS)
    await app._disk_added(d)
    r = await c.post(
        "/api/v1/devices/register",
        json={
            "name": "Handheld",
            "disk": d.path,
            "on_connect": "manual",
            "settings": {"base_path": "Music/SyncMusic"},
        },
    )
    dev = r.json()["id"]
    device = await app.sync.open_device(dev)
    await device.check()
    ours = storage / device.s.base_path
    (ours / "a.mp3").write_bytes(b"x")
    r = await c.post(f"/api/v1/devices/{dev}/wipe", json={"confirm": "Handheld"})
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded", job.error
    assert job.result["type"] == "android" and job.result["removed"] == 1
    assert (storage / "DCIM" / "photo.jpg").exists() and not (ours / "a.mp3").exists()


async def test_found_player_storages_and_folders_for_register(env, tmp_path):
    """Register lists a found player's storages and browses its folders over MTP; the settings
    chosen there (storage, the root as base folder) are kept, the serial comes from udev."""
    app, c = env
    (tmp_path / "storage" / "Music" / "Albums").mkdir(parents=True)
    (tmp_path / "storage" / "Music" / "a.mp3").write_bytes(b"x")
    d = mtp_info("1-1", PROPS)
    await app._disk_added(d)
    r = await c.get("/api/v1/discovery/mtp-storages", params={"disk": d.path})
    assert r.status_code == 200, r.text
    assert [s["name"] for s in r.json()] == ["Internal shared storage"] and r.json()[0][
        "capacity"
    ] > 0
    r = await c.post("/api/v1/browse", json={"source": "mtp", "disk": d.path, "path": "/"})
    assert r.status_code == 200, r.text
    assert r.json()["folders"] == ["Music"] and r.json()["parent"] is None
    r = await c.post("/api/v1/browse", json={"source": "mtp", "disk": d.path, "path": "/Music"})
    assert (r.json()["folders"], r.json()["media_files"]) == (["Albums"], 1)
    r = await c.post("/api/v1/browse", json={"source": "mtp", "disk": d.path, "path": "/nope"})
    assert r.status_code == 404, r.text
    r = await c.get("/api/v1/discovery/mtp-storages", params={"disk": "/dev/sdz"})
    assert r.status_code == 404, r.text
    r = await c.post(
        "/api/v1/devices/register",
        json={
            "name": "Handheld",
            "disk": d.path,
            "on_connect": "manual",
            "settings": {
                "storage": "Internal shared storage",
                "base_path": "./",
                "usb_serial": "FORGED",
            },
        },
    )
    assert r.status_code == 201, r.text
    s = r.json()["settings"]
    assert (s["usb_serial"], s["storage"]) == (SERIAL, "Internal shared storage")
    assert AndroidMtpSettings(**s).base_path == ""


async def test_overlapping_found_player_requests_queue_on_one_session(
    config, tmp_path, monkeypatch
):
    """libmtp allows one session per player, so parallel /browse calls of the same daemon failed with
    "another MTP client holds it"; they must queue instead."""
    import asyncio
    import time

    storage = tmp_path / "storage"
    (storage / "Music").mkdir(parents=True)
    open_now: list[FakeMtpBackend] = []

    class OneSession(FakeMtpBackend):
        def list(self, *a, **k):  # type: ignore[no-untyped-def]
            time.sleep(0.02)  # widen the window for an overlap
            return super().list(*a, **k)

        def close(self) -> None:
            open_now.remove(self)
            super().close()

    def factory(s, *_):
        if open_now:
            raise MtpUnavailable("the player is busy: another MTP client holds it")
        open_now.append(OneSession(storage))
        return open_now[-1]

    monkeypatch.setattr(AndroidMtp, "backend_factory", staticmethod(factory))
    app = Application(config)
    await app.start()
    try:
        d = mtp_info("1-1", PROPS)
        await app._disk_added(d)
        api = create_api(AppFacade(app), manage_lifecycle=False)
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
            rs = await asyncio.gather(
                *(
                    c.post(
                        "/api/v1/browse", json={"source": "mtp", "disk": d.path, "path": "/Music"}
                    )
                    for _ in range(6)
                ),
                c.get("/api/v1/discovery/mtp-storages", params={"disk": d.path}),
            )
        assert [r.status_code for r in rs] == [200] * 7, [r.text for r in rs]
        assert not open_now
    finally:
        await app.stop()

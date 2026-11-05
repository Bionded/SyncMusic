"""Android (MTP) device specifics over the fake backend: the default libmtp backend, the allowlist (624.3), a busy
player, the storage choice, an interrupted session cleaned up by the next, wipe limited to the base folder."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from syncmusic.core import registry
from syncmusic.core.models import DeviceConfig, Manifest, ManifestEntry, ManifestHeader
from syncmusic.devices.Android.mtp import AndroidMtp, AndroidMtpSettings, MtpUnavailable
from syncmusic.infra.blockdev.guard import Allowlist, GuardError
from tests.contracts.harness import work_item
from tests.fake_libmtp import FakeLibMtp
from tests.fake_mtp import FakeMtpBackend


def _device(tmp_path, **settings) -> tuple[AndroidMtp, FakeMtpBackend]:
    storage = tmp_path / "storage"
    storage.mkdir(exist_ok=True)
    backend = FakeMtpBackend(storage)
    cfg = DeviceConfig(
        id="a",
        type="android",
        name="A",
        settings={"usb_serial": "S1", "base_path": "Music/SyncMusic", **settings},
    )
    dev = AndroidMtp(cfg, storage / "Music" / "SyncMusic", None)
    dev.backend_factory = lambda s, *_: backend  # type: ignore[method-assign]
    return dev, backend


def test_registered_and_not_detected():
    assert registry.device_class("android") is AndroidMtp
    assert AndroidMtp.network and AndroidMtp.probe(None, None) is None
    caps = AndroidMtp.capabilities
    assert caps.formats == {"mp3", "aac", "flac", "ogg", "opus"}
    assert not (
        caps.supports_playcounts_read or caps.supports_ratings_read or caps.supports_positions_read
    )


def test_settings():
    s = AndroidMtpSettings(usb_serial=" X ", base_path="/Music//Sync/")
    assert (s.usb_serial, s.base_path, s.storage) == ("X", "Music/Sync", None)
    for root in ("", "/", "./", "."):  # the storage's root
        assert AndroidMtpSettings(usb_serial="x", base_path=root).base_path == ""
    for bad in (
        {"usb_serial": ""},
        {"usb_serial": "x", "base_path": "a/../b"},
        {"usb_serial": "x", "host": "h"},
    ):
        with pytest.raises(ValidationError):
            AndroidMtpSettings.model_validate(bad)


def test_default_backend_is_libmtp():
    with (
        patch("syncmusic.devices.Android._libmtp.CLib", return_value=FakeLibMtp()),
        pytest.raises(MtpUnavailable, match="no MTP player found"),
    ):
        AndroidMtp.backend_factory(AndroidMtpSettings(usb_serial="S1"))


async def test_allowlist_refuses_unlisted_serial(tmp_path):
    dev, backend = _device(tmp_path)
    dev.ctx = SimpleNamespace(guard=SimpleNamespace(allowlist=Allowlist(frozenset({"OTHER"}))))
    with pytest.raises(GuardError, match="allowlist"):
        await dev.check()
    dev.ctx = SimpleNamespace(guard=SimpleNamespace(allowlist=Allowlist(frozenset({"S1"}))))
    await dev.check()


async def test_busy_player(tmp_path):
    dev, backend = _device(tmp_path)
    await dev.check()
    backend.busy = True
    with pytest.raises(MtpUnavailable, match="busy"):
        await dev.free_space()
    await dev.close()
    assert backend.closed


async def test_storage_choice(tmp_path):
    dev, _ = _device(tmp_path, storage="SD card")
    with pytest.raises(MtpUnavailable, match="Internal shared storage"):
        await dev.check()
    dev, _ = _device(tmp_path, storage="Internal shared storage")
    await dev.check()
    assert dev.mount.is_dir()


async def test_interrupted_session_cleaned_up_by_the_next(tmp_path):
    dev, _ = _device(tmp_path)
    s = await dev.begin()
    ref = await dev.put_track(s, work_item(tmp_path, "Зірка", artist="Скрябін"))
    assert (dev.mount / ref.path).is_file()  # crash: no commit, no abort
    s2 = await dev.begin()
    assert s2.data["inflight_removed"] == [ref.path]
    assert not (dev.mount / ref.path).exists()


async def test_wipe_only_our_folder(tmp_path):
    dev, backend = _device(tmp_path)
    other = backend.root / "Music" / "Mine.mp3"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"x")
    s = await dev.begin()
    ref = await dev.put_track(s, work_item(tmp_path, "Зозуля", artist="Скрябін"))
    await dev.commit(s, Manifest(header=ManifestHeader(device_id="a")))
    assert await dev.wipe() == 1
    assert not (dev.mount / ref.path).exists() and await dev.read_manifest() is None
    assert other.is_file()


async def test_root_base_folder_wipes_only_our_files(tmp_path):
    """the storage's root as base folder; Wipe deletes only what our manifest lists, the
    Android/ folder is not walked."""
    dev, backend = _device(tmp_path, base_path="./")
    dev.mount = backend.root
    for rel in ("Music/Mine.mp3", "Android/data/app/x.mp3", "DCIM/a.jpg"):
        (backend.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (backend.root / rel).write_bytes(b"x")
    s = await dev.begin()
    assert not any(p.startswith("Android/") for p in s.data["paths"].values())
    item = work_item(tmp_path, "Зозуля", artist="Скрябін")
    ref = await dev.put_track(s, item)
    assert (backend.root / ref.path).is_file()
    entry = ManifestEntry(
        ref=item.plan.track.ref, device_ref=ref.device_ref, path=ref.path, size=ref.size
    )
    await dev.commit(
        s, Manifest(header=ManifestHeader(device_id="a"), items={item.plan.track.ref.key(): entry})
    )
    assert (backend.root / ".syncmusic").is_dir()
    contents = await dev.read_contents()
    assert all(not t.path.startswith("Android/") for t in contents.items)
    assert await dev.wipe() == 1
    assert not (backend.root / ref.path).exists() and await dev.read_manifest() is None
    for rel in ("Music/Mine.mp3", "Android/data/app/x.mp3", "DCIM/a.jpg"):
        assert (backend.root / rel).is_file(), rel


def test_open_player_checks_the_allowlist(tmp_path, monkeypatch):
    backend = FakeMtpBackend(tmp_path)
    monkeypatch.setattr(AndroidMtp, "backend_factory", staticmethod(lambda s, *_: backend))
    assert AndroidMtp.open_player("S1", frozenset({"S1"})) is backend
    assert AndroidMtp.open_player("S1", None) is backend
    with pytest.raises(GuardError):
        AndroidMtp.open_player("S2", frozenset({"S1"}))


def test_hidden_and_refused_without_libmtp(monkeypatch):
    """without libmtp the type is not offered and registering it fails clearly."""
    import ctypes.util

    from syncmusic import facade
    from syncmusic.devices.Android import mtp as android_mtp

    monkeypatch.setattr(
        android_mtp.AndroidMtp, "backend_factory", staticmethod(android_mtp._libmtp_backend)
    )
    real = ctypes.util.find_library
    monkeypatch.setattr(ctypes.util, "find_library", lambda n: None if n == "mtp" else real(n))
    assert "libmtp is not installed" in android_mtp.AndroidMtp.unavailable_reason()
    assert facade._type_unavailable("android")
    monkeypatch.setattr(ctypes.util, "find_library", real)
    if real("mtp"):
        assert android_mtp.AndroidMtp.unavailable_reason() is None

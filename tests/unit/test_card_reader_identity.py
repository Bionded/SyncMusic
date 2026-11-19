"""A card in a card reader: detection matches it by manifest or fs UUID, the reader has its own serial."""
# ruff: noqa: F811 - pytest fixtures imported from test_api_sync

from __future__ import annotations

import pytest

from syncmusic.core.detection import ConnectedDisk
from syncmusic.core.models import DeviceConfig, DeviceIdentity
from syncmusic.infra.blockdev import guard as guard_mod
from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.unit.test_api_sync import env, music  # noqa: F401


def _reader_disk(serial: str, uuid: str) -> BlockDeviceInfo:
    part = BlockDeviceInfo(name="sdz1", path="/dev/sdz1", type="part", fstype="vfat", uuid=uuid)
    return BlockDeviceInfo(
        name="sdz",
        path="/dev/sdz",
        type="disk",
        tran="usb",
        rm=True,
        serial=serial,
        children=[part],
    )


class _Guard:
    """The real target check against a fixed disk list."""

    def __init__(self, disk: BlockDeviceInfo) -> None:
        self.disks = [disk]

    def check(self, target, operation, registered=None, disks=None):
        return guard_mod.check_target(
            target, self.disks, registered=registered, operation=operation
        )


async def _setup(app, identity: DeviceIdentity, disk: BlockDeviceInfo, matched: bool):
    cfg = DeviceConfig(id="card", type="hiby_compas", name="Card", identity=identity)
    await app.store.put_device(cfg)
    await app.sync.load()
    app.sync.detector.connected["sdz"] = ConnectedDisk(
        disk=disk,
        device_id="card" if matched else None,
        matched_by="manifest" if matched else None,
        data_partition="/dev/sdz1",
    )
    app.sync.ctx.guard = _Guard(disk)  # type: ignore[assignment]
    return cfg


@pytest.fixture
def ejected(monkeypatch):
    calls: list[str] = []

    async def eject(guard, disk_path, registered=None):
        guard.check(disk_path, "eject", registered)
        calls.append(disk_path)

    async def unmount(device_id):
        return None

    monkeypatch.setattr(mnt, "eject", eject)
    return calls, unmount


async def test_eject_of_a_card_matched_through_another_reader(env, ejected, monkeypatch):
    app, _ = env
    calls, unmount = ejected
    disk = _reader_disk(
        "READER-B", "AAAA-0002"
    )  # registered over USB as serial A with another fs UUID
    cfg = await _setup(app, DeviceIdentity(usb_serial="PLAYER-A", fs_uuid="AAAA-0001"), disk, True)
    monkeypatch.setattr(app.sync, "_unmount", unmount)
    await app.sync.eject(cfg.id)
    assert calls == ["/dev/sdz"]
    assert "READER-B" in app.sync._guard_ids(cfg)


async def test_eject_of_a_card_matched_by_fs_uuid_on_the_partition(env, ejected, monkeypatch):
    app, _ = env
    calls, unmount = ejected
    disk = _reader_disk("READER-B", "AAAA-0001")
    cfg = await _setup(app, DeviceIdentity(usb_serial="PLAYER-A", fs_uuid="AAAA-0001"), disk, True)
    monkeypatch.setattr(app.sync, "_unmount", unmount)
    await app.sync.eject(cfg.id)
    assert calls == ["/dev/sdz"]


async def test_a_disk_detection_did_not_match_is_still_refused(env):
    app, _ = env
    disk = _reader_disk("OTHER", "BBBB-0001")
    cfg = await _setup(app, DeviceIdentity(usb_serial="PLAYER-A", fs_uuid="AAAA-0001"), disk, False)
    assert app.sync._guard_ids(cfg) == ["PLAYER-A", "AAAA-0001"]
    with pytest.raises(guard_mod.GuardError):
        app.sync.ctx.guard.check("/dev/sdz", "eject", app.sync._guard_ids(cfg))

"""a device added by directory can never be formatted. A directory that is the mount of a detected disk
registers the disk; an existing directory-bound device can be bound to its disk (keeping its manifest id)."""
# ruff: noqa: F811 - pytest fixtures imported from test_api_sync

from __future__ import annotations

from pathlib import Path

from syncmusic.core.detection import ConnectedDisk
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.unit.test_api_sync import _register, _removable_true, env, music  # noqa: F401


def _connect(app, mount, serial="S7", uuid="AB12-CD34"):
    """A Zorvex-like disk whose vfat partition is mounted (by the desktop) at `mount`."""
    part = BlockDeviceInfo(
        name="sdy1",
        path="/dev/sdy1",
        type="part",
        fstype="vfat",
        uuid=uuid,
        mountpoints=[str(mount)],
    )
    disk = BlockDeviceInfo(
        name="sdy", path="/dev/sdy", tran="usb", rm=True, serial=serial, children=[part]
    )
    return disk


async def test_register_by_mounted_directory_registers_the_disk(env, tmp_path):
    app, c = env
    mount = tmp_path / "media" / "ZORVEX"
    mount.mkdir(parents=True)
    await app.sync.detector.disk_added(_connect(app, mount))
    r = await c.post(
        "/api/v1/devices/register",
        json={
            "name": "Stick",
            "mount_path": str(mount),
            "type": "flashdrive",
            "library_ids": ["music"],
        },
    )
    assert r.status_code == 201, r.text
    cfg = r.json()
    assert cfg["mount_path"] is None
    assert cfg["identity"]["usb_serial"] == "S7"
    (dev,) = (await c.get("/api/v1/devices")).json()["registered"]
    assert dev["kind"] == "disk" and dev["can_format"] and dev["disk"] == "/dev/sdy"


async def test_plain_folder_stays_a_folder_device_without_format(env, monkeypatch, tmp_path):
    app, c = env
    monkeypatch.setattr(type(app.sync), "_removable", _removable_true)
    cfg = await _register(c, tmp_path / "folder")
    assert cfg["mount_path"]
    (dev,) = (await c.get("/api/v1/devices")).json()["registered"]
    assert dev["kind"] == "folder" and not dev["can_format"] and dev["bindable_disk"] is None
    r = await c.post(f"/api/v1/devices/{cfg['id']}/format", json={"confirm": cfg["name"]})
    assert r.status_code >= 400
    r = await c.post(f"/api/v1/devices/{cfg['id']}/bind-disk")
    assert r.status_code >= 400, r.text


async def test_existing_folder_device_is_bound_to_its_disk(env, monkeypatch, tmp_path):
    """Migration: a device registered by directory keeps its manifest id and gains the
    disk identity; afterwards it is matched by serial and Format is allowed."""
    app, c = env
    monkeypatch.setattr(type(app.sync), "_removable", _removable_true)
    mount = tmp_path / "media" / "ZORVEX"
    cfg = await _register(c, mount)  # no disk detected yet: a folder device
    manifest_id = cfg["identity"]["manifest_device_id"]
    await app.sync.detector.disk_added(_connect(app, mount))
    (dev,) = (await c.get("/api/v1/devices")).json()["registered"]
    assert dev["kind"] == "folder" and dev["bindable_disk"] == "/dev/sdy"
    r = await c.post(f"/api/v1/devices/{cfg['id']}/bind-disk")
    assert r.status_code == 200, r.text
    bound = r.json()
    assert bound["mount_path"] is None
    assert bound["identity"]["usb_serial"] == "S7"
    assert bound["identity"]["manifest_device_id"] == manifest_id
    cd: ConnectedDisk = app.sync.detector.connected["sdy"]
    assert cd.device_id == cfg["id"] and cd.matched_by == "usb_serial"
    (dev,) = (await c.get("/api/v1/devices")).json()["registered"]
    assert dev["kind"] == "disk" and dev["can_format"]


def test_mount_source_reads_the_kernel_mount_table(tmp_path):
    """A mount made after detection (no block uevent) is still found: the source comes from mountinfo."""
    from syncmusic.infra.blockdev import mount as mnt

    info = tmp_path / "mountinfo"
    info.write_text(
        "22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw\n"
        "90 22 0:50 / /media/u/MY\\040STICK rw,relatime shared:5 - fuseblk /dev/sdc1 rw,user_id=0\n"
    )
    assert mnt.mount_source(tmp_path / "x", str(info)) is None
    assert mnt.mount_source(Path("/media/u/MY STICK"), str(info)) == "/dev/sdc1"

"""Device settings panel backend: the iPod's time zone (clock payload, play times, validation), which file
systems this host can create (missing tool / driver → unavailable with the reason), the choice per device type, and
the format API refusing what the type or the host cannot do."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from syncmusic import facade as facade_mod
from syncmusic.devices.iPod.ipod import IPodSettings, _local_wall_to_utc
from syncmusic.infra.blockdev import format as fmt
from syncmusic.infra.blockdev import scsi
from tests.unit.test_api_sync import _register, env, music  # noqa: F401 - fixtures
from tests.unit.test_time_sync import tz  # noqa: F401 - fixture


def test_clock_payload_in_the_chosen_zone_whatever_the_host_zone(tz):
    tz("Europe/Berlin")  # the host zone must not matter once a zone is set
    now = dt.datetime(2026, 7, 1, 12, 0, 0, tzinfo=dt.UTC)
    ny = scsi.ipod_time_payload(now, "America/New_York")  # EDT, UTC-4: 08:00, dst
    assert ny == bytes([0x07, 0xEA, 0x00, 181, (-16) & 0xFF, 8, 0, 0, 1, 0, 0, 0])
    india = scsi.ipod_time_payload(now, "Asia/Kolkata")  # UTC+5:30, no dst
    assert india == bytes([0x07, 0xEA, 0x00, 181, 22, 17, 30, 0, 0, 0, 0, 0])
    assert scsi.ipod_time_payload(now, None) == scsi.ipod_time_payload(now)  # None = host zone


def test_play_times_are_read_in_the_zone():
    wall = dt.datetime(2026, 1, 10, 20, 0, 0)  # what the iPod recorded (its wall clock)
    assert _local_wall_to_utc(wall, "Asia/Tokyo") == dt.datetime(2026, 1, 10, 11, 0, tzinfo=dt.UTC)


def test_timezone_setting_validation():
    assert IPodSettings(timezone=" Europe/Lisbon ").timezone == "Europe/Lisbon"
    assert IPodSettings(timezone="").timezone is None
    with pytest.raises(ValidationError, match="unknown time zone"):
        IPodSettings(timezone="Mars/Olympus")
    schema = IPodSettings.model_json_schema()["properties"]["timezone"]
    assert "Europe/Lisbon" in schema["examples"]  # the form's suggestions


def test_filesystem_support_names_what_is_missing():
    have = {"mkfs.vfat", "mkfs.exfat", "mkfs.ext4"}
    out = fmt.filesystem_support(
        which=lambda tool: f"/sbin/{tool}" if tool in have else None,
        kernel_has=lambda fs: fs != "exfat",
    )
    assert out["vfat"] is None and out["ext4"] is None
    assert out["exfat"] == "the kernel has no exfat driver"
    assert out["ntfs"] == "mkfs.ntfs is not installed (package ntfs-3g)"


def test_kernel_probe_reads_proc_and_modules(tmp_path):
    proc = tmp_path / "filesystems"
    proc.write_text("nodev\tsysfs\n\tvfat\n\text4\n")
    mods = tmp_path / "mods"
    (mods / "kernel/fs/ntfs3").mkdir(parents=True)
    (mods / "kernel/fs/ntfs3/ntfs3.ko.xz").write_bytes(b"")
    assert fmt._kernel_has("vfat", proc, mods) and fmt._kernel_has("ntfs3", proc, mods)
    assert not fmt._kernel_has("exfat", proc, mods)


async def test_types_list_their_file_systems(env, monkeypatch):
    _, c = env
    monkeypatch.setattr(
        facade_mod,
        "filesystem_support",
        lambda: {**dict.fromkeys(fmt.FILESYSTEMS), "ntfs": "no ntfs"},
    )
    types = {t["type"]: t for t in (await c.get("/api/v1/device-types")).json()}
    flash = types["flashdrive"]["format_filesystems"]
    assert [f["label"] for f in flash] == ["FAT32", "exFAT", "ext4", "NTFS"]
    assert flash[3] == {"fs": "ntfs", "label": "NTFS", "available": False, "reason": "no ntfs"}
    assert [f["fs"] for f in types["ipod"]["format_filesystems"]] == [
        "vfat"
    ]  # data partition, FAT32 only
    assert [f["fs"] for f in types["rockbox"]["format_filesystems"]] == ["vfat"]  # no choice


async def test_format_refuses_a_file_system_the_type_or_host_cannot_make(
    env, tmp_path, monkeypatch
):
    _, c = env
    dev = await _register(c, tmp_path / "stick", name="Stick")
    monkeypatch.setattr(
        facade_mod,
        "filesystem_support",
        lambda: {**dict.fromkeys(fmt.FILESYSTEMS), "ntfs": "no ntfs"},
    )
    r = await c.post(f"/api/v1/devices/{dev['id']}/format", json={"confirm": "Stick", "fs": "ntfs"})
    assert r.status_code == 400 and "no ntfs" in r.json()["message"]
    r = await c.post(f"/api/v1/devices/{dev['id']}/format", json={"confirm": "Stick", "fs": "zfs"})
    assert r.status_code == 400 and "FAT32, exFAT, ext4, NTFS" in r.json()["message"]


async def test_timezone_is_saved_and_checked_by_the_api(env, tmp_path):
    app, c = env
    dev = await _register(c, tmp_path / "pod", name="Pod", type="ipod")
    url = f"/api/v1/devices/{dev['id']}"
    r = await c.patch(url, json={"settings": {**dev["settings"], "timezone": "Mars/Olympus"}})
    assert r.status_code == 400 and "unknown time zone" in r.text
    r = await c.patch(
        url, json={"settings": {**dev["settings"], "time_sync": True, "timezone": "Asia/Tokyo"}}
    )
    assert r.status_code == 200, r.text
    stored = await app.store.device(dev["id"])
    assert stored.settings["timezone"] == "Asia/Tokyo" and stored.settings["time_sync"] is True

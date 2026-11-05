"""On hardware, once per flash drive and per iPod Classic: Rename. The disk is formatted (FAT32; a Classic's data
partition only) and gets 2 songs; Rename changes the on-device name (a Classic's database name and, as Format gave
it one, its FAT label; a flash drive's FAT label), re-read from the disk; the songs, the manifest and a Classic's
partition table stay; no sync job runs. Then the old name is set back. The disk is left unmounted, not ejected.
"""

from __future__ import annotations

import pytest

from syncmusic.core.sync import RegisterRequest
from syncmusic.infra.blockdev.identify import find_disk_of, scan_block_devices
from tests.factories import own_rules
from tests.hardware.test_classic_acceptance import _data_listing, _partition_table
from tests.hardware.test_player_contents import app  # noqa: F401 - fixture
from tests.integration.test_navidrome_local import ENV

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif("NAVIDROME_URL" not in ENV, reason="no local Navidrome in .env.test"),
]

TWO = {
    "rules": [
        {
            "id": "two",
            "name": "Two",
            "criteria": {"is": {"kind": "music"}},
            "limit": 2,
            "sort": "title",
        }
    ]
}


async def test_rename(app):  # noqa: F811
    await rename_scenario(*app)


async def rename_scenario(a, disks):
    """Rename a flash drive and a stock iPod (also on emulated devices, test_emulated_two_devices.py)."""
    from pygpod import Database

    before = _data_listing()
    for disk in disks:
        ipod = disk.usb_vendor_id == "05ac"
        name = "Rename Classic" if ipod else "Rename Stick"
        table = _partition_table(disk.path)
        cfg = await a.sync.register(
            RegisterRequest(
                name=name,
                disk=disk.path,
                rule_set=await own_rules(a, name, TWO, ["nd"]),
                on_connect="manual",
            )
        )
        cfg = await a.sync.format(cfg.id, confirm=name, fs=None if ipod else "vfat")
        pc = await a.sync.plan(cfg.id)
        job = await a.jobs.wait((await a.sync.apply(pc.plan.id, first_sync="keep")).id)
        assert job.status == "succeeded", job.error
        rows = await a.sync.contents(cfg.id)
        assert len(rows) == 2
        jobs = len(a.jobs.jobs())

        preview = await a.sync.rename_preview(cfg.id, "Домашній плеєр 2")
        assert preview["label"] == "2" and preview["fs"] == "vfat"  # FAT: upper-case ASCII only
        new, label = await a.sync.rename(cfg.id, "Домашній плеєр 2")
        assert new.name == "Домашній плеєр 2" and label == "2"
        part = a.sync.detector.by_device(cfg.id).data_partition
        assert find_disk_of(part, scan_block_devices(False))[1].label == "2"
        dev = await a.sync.open_device(cfg.id)  # mounted again
        if ipod:
            db = Database(str(dev.mount), apply_play_counts=False)
            assert db.master_playlist.name == "Домашній плеєр 2" and len(db.tracks) == 2
        assert {r["key"] for r in await a.sync.contents(cfg.id)} == {r["key"] for r in rows}
        assert all((dev.mount / r["path"]).is_file() for r in rows)
        assert len(a.jobs.jobs()) == jobs  # not a sync
        assert _partition_table(disk.path) == table or not ipod

        cfg, label = await a.sync.rename(cfg.id, name)  # back
        assert label == name.upper()[:11].strip()
        if ipod:
            dev = await a.sync.open_device(cfg.id)
            assert Database(str(dev.mount), apply_play_counts=False).master_playlist.name == name
            assert _partition_table(disk.path) == table
    assert _data_listing() == before, "the test media changed"

"""On hardware, once per flash drive and per iPod Classic: the device gets 5 songs from the local Navidrome, then
2 of them are removed through Player contents. Only those 2 files go, the rest stays; a Classic's database (re-read
with pygpod) no longer has them, and a new plan does not bring them back.

The disk is formatted (FAT32; a Classic's data partition only) and left unmounted, not ejected.
"""

from __future__ import annotations

import pytest

from syncmusic.app import Application
from syncmusic.core.models import ActionKind, LibraryConfig
from syncmusic.core.sync import RegisterRequest
from tests.factories import own_rules
from tests.hardware.test_classic_acceptance import (
    _check_db,
    _data_listing,
    _partition_table,
)
from tests.integration.test_navidrome_local import ENV

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif("NAVIDROME_URL" not in ENV, reason="no local Navidrome in .env.test"),
]

FIVE = {
    "rules": [
        {
            "id": "five",
            "name": "Five songs",
            "criteria": {"is": {"kind": "music"}},
            "limit": 5,
            "sort": "title",
        }
    ]
}


@pytest.fixture
async def app(config, flash_or_classic_disk):
    found = [flash_or_classic_disk]
    a = Application(config)
    await a.start()
    await a.store.put_library(
        LibraryConfig(
            id="nd",
            type="navidrome",
            name="Navidrome",
            settings={
                "url": ENV["NAVIDROME_URL"],
                "username": ENV["NAVIDROME_USER"],
                "password": ENV["NAVIDROME_PASSWORD"],
            },
        )
    )
    for disk in found:
        await a._disk_added(disk)
    yield a, found
    await a.stop()


async def test_remove_two_tracks_through_player_contents(app):
    await contents_scenario(*app)


async def contents_scenario(a, disks):
    """Remove 2 of 5 songs through Player contents (also on emulated devices, test_emulated_two_devices.py)."""
    before = _data_listing()
    for disk in disks:
        ipod = disk.usb_vendor_id == "05ac"
        name = "Contents Classic" if ipod else "Contents Stick"
        table = _partition_table(disk.path) if ipod else None
        cfg = await a.sync.register(
            RegisterRequest(
                name=name,
                disk=disk.path,
                rule_set=await own_rules(a, name, FIVE, ["nd"]),
                on_connect="manual",
            )
        )
        cfg = await a.sync.format(cfg.id, confirm=name, fs=None if ipod else "vfat")
        pc = await a.sync.plan(cfg.id)
        job = await a.jobs.wait((await a.sync.apply(pc.plan.id, first_sync="keep")).id)
        assert job.status == "succeeded", job.error

        rows = await a.sync.contents(cfg.id)
        assert len(rows) == 5 and not any(r["orphan"] for r in rows)
        gone = rows[:2]
        dev = await a.sync.open_device(cfg.id)
        paths = [dev.mount / r["path"] for r in gone]
        assert all(p.is_file() for p in paths)
        job = await a.jobs.wait(
            (await a.sync.remove_from_device(cfg.id, [r["key"] for r in gone])).id
        )
        assert job.status == "succeeded", job.error
        assert job.result["counts"]["remove"] == 2 and job.result["counts"]["add"] == 0

        dev = await a.sync.open_device(cfg.id)
        assert not any(p.exists() for p in paths)
        left = await a.sync.contents(cfg.id)
        assert {r["key"] for r in left} == {r["key"] for r in rows[2:]}
        if ipod:
            _, m, db = await _check_db(a, cfg.id)
            assert len(db.tracks) == len(m.items) == 3
            assert _partition_table(disk.path) == table
        pc = await a.sync.plan(cfg.id)  # excluded: they do not come back
        assert pc.plan.count(ActionKind.ADD) == 0, [
            x.title for x in pc.plan.actions if x.kind is ActionKind.ADD
        ]
    assert _data_listing() == before, "the test media changed"

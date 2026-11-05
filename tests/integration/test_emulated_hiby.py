"""HiBy R1 / SonixOS on an emulated whole-disk exFAT image: detected as `hiby`, Format offers exFAT
(default), FAT32 and NTFS on the whole disk (no partition table), the player's tree mounts again after each one and a
small sync lands on it."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from syncmusic.core.models import ActionKind
from syncmusic.core.sync import RegisterRequest
from tests.emulated import attach, block_device, navidrome_config, needs_sudo, running_app, sh
from tests.factories import own_rules

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]

FEW = {
    "rules": [
        {"id": "few", "name": "Two tracks", "criteria": {"all": []}, "limit": 2, "sort": "title"}
    ]
}


def _fstype(mount: Path) -> str:
    target = os.path.realpath(mount)
    for line in Path("/proc/self/mounts").read_text().splitlines():
        f = line.split()
        if f[1] == target:
            return f[2]
    return ""


async def test_hiby_format_every_filesystem_and_sync(services, tmp_path):
    with attach("emulated_hiby", 256) as disk:
        sh("sudo", "-n", "mkfs.exfat", "-L", "R1", disk.dev)
        sh("udevadm", "settle", "--timeout=10")
        assert block_device(disk).partitions() == []
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            cfg = await app.sync.register(
                RegisterRequest(
                    name="R1",
                    disk=blk.path,
                    rule_set=await own_rules(app, "R1", FEW, ["nd"]),
                    on_connect="manual",
                )
            )
            assert cfg.type == "hiby", cfg.type
            dev = await app.sync.open_device(cfg.id)
            local = dev.mount / ".local"
            (local / "eq").mkdir(parents=True, exist_ok=True)
            (local / "eq" / "mine.ini").write_text("bass=3")
            con = sqlite3.connect(local / "library.db")
            con.execute(
                "CREATE TABLE IF NOT EXISTS FAVOURITES(path TEXT PRIMARY KEY, name, artist, added_at)"
            )
            con.execute("INSERT INTO FAVOURITES VALUES ('/mnt/sd_0/Music/x.flac', 'x', '', 1)")
            con.commit()
            con.close()
            # no ntfs: kernel ntfs3 on a loop image deadlocks writeback until reboot
            for fs, kernel in (("vfat", "vfat"), ("exfat", "exfat")):
                cfg = await app.sync.format(cfg.id, confirm="R1", fs=fs)
                assert block_device(disk).partitions() == []  # still one whole-disk volume
                dev = await app.sync.open_device(cfg.id)
                assert kernel in _fstype(dev.mount), (fs, _fstype(dev.mount))
                assert cfg.identity.fs_uuid
                # the player's settings survive, its index starts empty with the SonixOS schema
                local = dev.mount / ".local"
                assert (local / "eq" / "mine.ini").read_text() == "bass=3", fs
                con = sqlite3.connect(local / "library.db")
                assert con.execute("SELECT COUNT(*) FROM FAVOURITES").fetchone() == (0,)
                con.close()
                assert (local / "audiobooks.db").is_file() and (local / "podcast.db").is_file()
            cfg = await app.sync.update_device(
                cfg.model_copy(update={"rule_set": await own_rules(app, cfg.name, FEW, ["nd"])})
            )
            pc = await app.sync.plan(cfg.id)
            assert pc.plan.count(ActionKind.ADD) == 2, pc.plan.warnings
            job = await app.sync.apply(pc.plan.id, first_sync="keep")
            job = await app.jobs.wait(job.id)
            assert job.status == "succeeded", job.error
            assert not [w for w in (job.result or {}).get("warnings", []) if "player" in w], (
                job.result
            )
            dev = await app.sync.open_device(cfg.id)
            assert len((await dev.read_manifest()).items) == 2  # type: ignore[union-attr]
            # the index is rebuilt from the card after the sync - every row points to a file on it
            _assert_index_matches_card(dev.mount, 2)
            one = {"rules": [dict(FEW["rules"][0], limit=1, sort="-title")]}
            cfg = await app.sync.update_device(
                cfg.model_copy(update={"rule_set": await own_rules(app, cfg.name, one, ["nd"])})
            )
            pc = await app.sync.plan(cfg.id)
            job = await app.jobs.wait((await app.sync.apply(pc.plan.id)).id)
            assert job.status == "succeeded", job.error
            dev = await app.sync.open_device(cfg.id)
            _assert_index_matches_card(dev.mount, 1)  # no stale rows of the first sync


def _assert_index_matches_card(mount: Path, tracks: int) -> None:
    from syncmusic.devices.HibyR1.hiby_index import VOLUME

    con = sqlite3.connect(mount / ".local" / "library.db")
    try:
        paths = [p for (p,) in con.execute("SELECT path FROM MEDIA_TABLE")]
        assert len(paths) == tracks, paths
        for p in paths:
            assert (mount / p.removeprefix(VOLUME + "/")).is_file(), p
        assert con.execute("SELECT cn FROM COUNT_TABLE").fetchall() == [(tracks,)]
        for (p,) in con.execute("SELECT path FROM FOLDER_TABLE"):
            assert (mount / p.removeprefix(VOLUME).lstrip("/")).is_dir(), p
        names = {n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "media_path_idx" in names, names
    finally:
        con.close()

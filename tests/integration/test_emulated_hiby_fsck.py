"""HiBy R1 on emulated exFAT and FAT32 images: after a sync, repeated update syncs with nothing new leave
the player databases untouched on the card, and the file system checks clean after unmounting."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from syncmusic.core.models import ActionKind
from syncmusic.core.sync import RegisterRequest
from tests.emulated import attach, navidrome_config, needs_sudo, running_app, sh
from tests.factories import own_rules

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]

FEW = {
    "rules": [
        {"id": "few", "name": "Three tracks", "criteria": {"all": []}, "limit": 3, "sort": "title"}
    ]
}


@pytest.mark.parametrize(
    ("mkfs", "fsck"),
    [
        (("mkfs.exfat", "-L", "R1"), ("fsck.exfat", "-n")),
        (("mkfs.vfat", "-F", "32", "-n", "R1"), ("fsck.vfat", "-n")),
    ],
    ids=["exfat", "fat32"],
)
async def test_updates_with_nothing_new_keep_databases_and_fs_clean(services, tmp_path, mkfs, fsck):
    with attach("emulated_hiby", 256) as disk:
        sh(*mkfs, disk.dev)
        sh("udevadm", "settle", "--timeout=10")
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            cfg = await app.sync.register(
                RegisterRequest(
                    name="R1",
                    disk=blk.path,
                    rule_set=await own_rules(app, "R1", FEW, ["nd"]),
                    on_connect="manual",
                )
            )
            pc = await app.sync.plan(cfg.id)
            assert pc.plan.count(ActionKind.ADD) == 3, pc.plan.warnings
            job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
            assert job.status == "succeeded", job.error

            dev = await app.sync.open_device(cfg.id)
            local = dev.mount / ".local"
            # FAT inode numbers change on every mount: compare mtime and bytes
            stamp = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in local.glob("*.db")}
            assert "library.db" in stamp
            for _ in range(5):
                job = await app.jobs.wait((await app.sync.update_sync(cfg.id)).id)
                assert job.status == "succeeded", job.error
            dev = await app.sync.open_device(cfg.id)
            local = dev.mount / ".local"
            after = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in local.glob("*.db")}
            assert after == stamp
            assert not list(local.glob("*.db-journal")) and not list(local.glob("*.new"))
            await app.sync._unmount(cfg.id)
        res = await asyncio.to_thread(
            subprocess.run,
            ["sudo", "-n", *fsck, disk.dev],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, res.stdout + res.stderr

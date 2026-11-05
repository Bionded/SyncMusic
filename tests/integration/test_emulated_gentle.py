"""Gentle write mode on an emulated flash drive with the compose stack's Navidrome: the FAT32
data partition is mounted without `flush`, a full sync in batches of 2 with a real `sync -f` succeeds."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncmusic.core.models import SyncSettings
from syncmusic.core.sync import RegisterRequest
from tests.emulated import attach, flash, navidrome_config, needs_sudo, running_app
from tests.factories import own_rules

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]


def _options(mount: Path) -> list[str]:
    for line in Path("/proc/self/mounts").read_text().splitlines():
        fields = line.split()
        if fields[1] == str(mount):
            return fields[3].split(",")
    raise AssertionError(f"{mount} not mounted")


async def test_gentle_flash_mounts_without_flush_and_syncs(services, tmp_path):
    with attach("emulated_flash", 256) as disk:
        await flash(disk, "vfat", "GENTLE")
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            rules = {"rules": [{"id": "m", "criteria": {"is": {"kind": "music"}}, "limit": 5}]}
            req = RegisterRequest(
                name="Gentle", disk=blk.path, rule_set=await own_rules(app, "Gentle", rules, ["nd"])
            )
            cfg = await app.sync.register(req)
            gentle = SyncSettings(gentle_write=True, gentle_batch_files=2, gentle_pause_s=0.1)
            cfg = await app.sync.update_device(cfg.model_copy(update={"sync": gentle}))
            dev = await app.sync.open_device(cfg.id)
            assert "flush" not in _options(dev.mount)
            pc = await app.sync.plan(cfg.id)
            job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
            assert job.status == "succeeded", job.error
            assert job.result["counts"]["add"] == 5
            assert len((await dev.read_manifest()).items) == 5

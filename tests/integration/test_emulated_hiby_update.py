"""HiBy R1 update syncs end to end on an emulated exFAT image:
an update with plays on the player sends them back and rewrites the databases once;
a manifest written with the old caps hash scheme is re-hashed, not replaced."""

from __future__ import annotations

import sqlite3

import pytest

from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import ActionKind
from syncmusic.core.prepare import CAPS_HASH_VERSION
from syncmusic.core.sync import RegisterRequest
from tests.emulated import attach, navidrome_config, needs_sudo, running_app, sh
from tests.factories import own_rules

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]

FEW = {
    "rules": [
        {"id": "few", "name": "Two tracks", "criteria": {"all": []}, "limit": 2, "sort": "title"}
    ]
}


async def test_hiby_update_rewrites_once_and_old_caps_hash_keeps_files(services, tmp_path):
    with attach("emulated_hiby", 256) as disk:
        sh("sudo", "-n", "mkfs.exfat", "-L", "R1", disk.dev)
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
            assert pc.plan.count(ActionKind.ADD) == 2, pc.plan.warnings
            job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
            assert job.status == "succeeded", job.error

            # an older SyncMusic's manifest: turn every entry into one of the old scheme (whole caps dump in the "device" part)
            dev = await app.sync.open_device(cfg.id)
            m = await dev.read_manifest()
            assert m is not None and len(m.items) == 2
            for e in m.items.values():
                assert e.caps_v == CAPS_HASH_VERSION and "device" in e.processing
                e.caps_v = 0
                e.processing = {**e.processing, "device": "0123456789abcdef"}
                e.chain_hash = "legacy-" + e.chain_hash
            manifest_io.write(dev.mount, m)
            # plays on the player before the update
            path = next(iter(m.items.values())).path
            con = sqlite3.connect(dev.mount / ".local" / "library.db")
            con.execute(f"INSERT INTO PLAY_COUNTS VALUES ('/mnt/sd_0/{path}', 3, 100)")
            con.commit()
            con.close()
            backups = dev._backup_dir()  # type: ignore[attr-defined]
            before = len(list(backups.glob("full-*")))

            pc = await app.sync.plan(cfg.id)
            kinds = [a.kind for a in pc.plan.actions]
            assert ActionKind.REPLACE not in kinds, [(a.kind, a.reason) for a in pc.plan.actions]
            assert not pc.plan.needs_confirmation, pc.plan.confirmation_reasons
            job = await app.jobs.wait((await app.sync.apply(pc.plan.id)).id)
            assert job.status == "succeeded", job.error
            assert len(list(backups.glob("full-*"))) == before + 1  # one delete + rewrite only

            dev = await app.sync.open_device(cfg.id)
            m = await dev.read_manifest()
            assert m is not None
            assert {e.caps_v for e in m.items.values()} == {CAPS_HASH_VERSION}
            assert all(not e.chain_hash.startswith("legacy-") for e in m.items.values())
            assert {e.path: e.last_playcount for e in m.items.values()}[path] == 3
            con = sqlite3.connect(dev.mount / ".local" / "library.db")
            assert con.execute("SELECT plays FROM PLAY_COUNTS").fetchall() == [(3,)]
            con.close()
            pc = await app.sync.plan(cfg.id)
            assert {a.kind for a in pc.plan.actions} == {ActionKind.KEEP}, pc.plan.actions

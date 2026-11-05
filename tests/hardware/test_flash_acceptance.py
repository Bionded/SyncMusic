"""Acceptance: the allowlisted Zorvex flash drive on vfat/exfat/ext4/ntfs, synced from
the local Navidrome (test media, read-only) through the real stack: guard + sudo mount/format, planner, applier.

Per file system: format → first sync (keep) → files/tags/manifest checked → re-run is a no-op → rule change
removes and adds → a tiny space limit gives does_not_fit → forced sync is partial with the skipped items listed.
Only the flash drive (matched by USB serial from tests/hardware.toml) is touched; it is not powered off at the end
(that would need a replug), only unmounted.
"""

from __future__ import annotations

import os
from pathlib import Path

import mutagen
import pytest

from syncmusic.app import Application
from syncmusic.core.models import ActionKind, LibraryConfig
from syncmusic.core.sync import RegisterRequest
from tests.factories import own_rules
from tests.integration.services import media_listing
from tests.integration.test_navidrome_local import ENV

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif("NAVIDROME_URL" not in ENV, reason="no local Navidrome in .env.test"),
]

FILESYSTEMS = ["vfat", "exfat", "ext4", "ntfs"]
ALL = {"rules": [{"id": "all", "name": "All music", "criteria": {"all": []}}]}
ONE_ARTIST_PLUS = {
    "rules": [
        {
            "id": "uv",
            "name": "Жадан і Собаки",
            "criteria": {"contains": {"artist": "Жадан і Собаки"}},
        },
        {
            "id": "rest",
            "name": "Two more",
            "criteria": {"notContains": {"artist": "Жадан і Собаки"}},
            "limit": 2,
            "sort": "title",
        },
    ]
}


def _data_listing() -> list[str]:
    return media_listing()


@pytest.fixture
async def app(config, flash_disk):
    flash = flash_disk
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
    await a._disk_added(flash)
    yield a, flash
    await a.stop()


async def _apply(a, plan_id, **kw):
    job = await a.sync.apply(plan_id, **kw)
    job = await a.jobs.wait(job.id)
    assert job.status == "succeeded", job.error
    return job.result


async def _check_device(a, device_id):
    dev = await a.sync.open_device(device_id)
    m = await dev.read_manifest()
    on_disk = {
        p.relative_to(dev.mount).as_posix()
        for p in dev.mount.rglob("*")
        if p.is_file()
        and not p.relative_to(dev.mount).as_posix().startswith(".syncmusic")
        and p.suffix.lower() in {".mp3", ".flac", ".m4a"}
    }
    assert on_disk == {e.path for e in m.items.values()}
    for e in m.items.values():
        f = dev.mount / e.path
        assert f.stat().st_size == e.size
        tags = mutagen.File(f, easy=True)
        assert tags is not None and tags.get("title"), e.path
    return dev, m


async def test_flash_every_filesystem(app):
    await flash_scenario(*app)


async def flash_scenario(a, flash):
    """The acceptance scenario on one flash drive (real here, emulated in tests/integration/test_emulated_flash.py)."""
    before = _data_listing()
    cfg = await a.sync.register(
        RegisterRequest(
            name="Zorvex",
            disk=flash.path,
            rule_set=await own_rules(a, "Zorvex", ALL, ["nd"]),
            on_connect="manual",
        )
    )
    assert cfg.type == "flashdrive" and cfg.identity.usb_serial == flash.serial
    for fs in FILESYSTEMS:
        cfg = await a.sync.format(cfg.id, confirm="Zorvex", fs=fs)
        dev = await a.sync.open_device(cfg.id)
        assert {"vfat": "vfat", "exfat": "exfat", "ext4": "ext4", "ntfs": "ntfs3"}[fs] in _fstype(
            dev.mount
        )
        cfg = await a.sync.update_device(
            cfg.model_copy(update={"rule_set": await own_rules(a, cfg.name, ALL, ["nd"])})
        )

        pc = await a.sync.plan(cfg.id)
        assert pc.plan.first_sync and pc.plan.count(ActionKind.ADD) >= 20, pc.plan.warnings
        rep = await _apply(a, pc.plan.id, first_sync="keep")
        assert rep["status"] == "succeeded" and not rep["failures"], rep
        _, m = await _check_device(a, cfg.id)
        assert len(m.items) == rep["counts"]["add"]

        pc = await a.sync.plan(cfg.id)  # re-run: no-op
        assert {x.kind for x in pc.plan.actions} == {ActionKind.KEEP}, [
            (x.kind, x.reason) for x in pc.plan.actions
        ]
        rep = await _apply(a, pc.plan.id)
        assert rep["bytes_written"] == 0

        cfg = await a.sync.update_device(
            cfg.model_copy(
                update={"rule_set": await own_rules(a, cfg.name, ONE_ARTIST_PLUS, ["nd"])}
            )
        )
        pc = await a.sync.plan(cfg.id)
        removes, adds = pc.plan.count(ActionKind.REMOVE), pc.plan.count(ActionKind.ADD)
        assert removes > 0 and adds == 0
        rep = await _apply(a, pc.plan.id, confirm=True)
        _, m = await _check_device(a, cfg.id)
        kept = len(m.items)
        cfg = await a.sync.update_device(
            cfg.model_copy(update={"rule_set": await own_rules(a, cfg.name, ALL, ["nd"])})
        )
        pc = await a.sync.plan(cfg.id)
        assert pc.plan.count(ActionKind.ADD) == removes  # the rule change back adds them again
        await _apply(a, pc.plan.id)

        # tiny space limit: a safety margin that leaves ~3 MB for the plan
        removed_back = await own_rules(a, cfg.name, ONE_ARTIST_PLUS, ["nd"])
        cfg = await a.sync.update_device(cfg.model_copy(update={"rule_set": removed_back}))
        pc = await a.sync.plan(cfg.id)
        await _apply(a, pc.plan.id, confirm=True)
        dev = await a.sync.open_device(cfg.id)
        free = await dev.free_space()
        cfg = await a.sync.update_device(
            cfg.model_copy(update={"rule_set": await own_rules(a, cfg.name, ALL, ["nd"])})
        )
        space = (await a.sync.plan(cfg.id)).plan.space
        # leave room for half of what the plan adds, at most 30 MB (small synthetic media)
        room = min(30 * 1000**2, (space.needed - space.reserve) // 2)
        tight = cfg.sync.model_copy(
            update={"safety_margin_pct": 0, "safety_margin_min_bytes": free - room}
        )
        cfg = await a.sync.update_device(cfg.model_copy(update={"sync": tight}))
        pc = await a.sync.plan(cfg.id)
        assert pc.plan.does_not_fit and pc.plan.space.suggestions
        pc = await a.sync.plan(cfg.id, force=True)
        assert not pc.plan.does_not_fit and pc.plan.space.dropped
        rep = await _apply(a, pc.plan.id)
        assert rep["skipped_for_space"] == pc.plan.space.dropped and rep["status"] == "succeeded"
        _, m = await _check_device(a, cfg.id)
        assert kept < len(m.items) < kept + removes
        cfg = await a.sync.update_device(
            cfg.model_copy(update={"sync": cfg.sync.model_validate({})})
        )
    assert _data_listing() == before, "the test media changed"


def _fstype(mount: Path) -> str:
    target = os.path.realpath(mount)
    for line in Path("/proc/self/mounts").read_text().splitlines():
        f = line.split()
        if f[1] == target:
            return f[2]
    return ""

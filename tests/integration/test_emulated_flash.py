"""Acceptance on an emulated flash drive: the scenario of tests/hardware/test_flash_acceptance.py - every
file system: format, first sync, no-op re-run, rule change, tiny space limit and forced partial sync - against the
compose stack's Navidrome. Then the mount vanishing mid-apply (tests/hardware/test_mount_lost.py) and the match by a
probe-mounted manifest (tests/hardware/test_probe_mount.py)."""

from __future__ import annotations

import pytest

from syncmusic.core.models import ActionKind, MediaKind
from syncmusic.core.sync import RegisterRequest
from tests.emulated import (
    abs_config,
    attach,
    block_device,
    emulated_guard,
    flash,
    grow,
    navidrome_config,
    needs_sudo,
    repair_vfat,
    replug,
    running_app,
    sh,
    small_backing_store,
    unplug,
)
from tests.factories import own_rules
from tests.hardware.test_flash_acceptance import flash_scenario
from tests.hardware.test_mount_lost import mount_lost_scenario
from tests.hardware.test_probe_mount import probe_manifest_scenario

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]


@pytest.mark.slow  # full acceptance scenario: -m emulated runs it
async def test_flash_every_filesystem(services, tmp_path):
    with attach("emulated_flash", 1024) as disk:
        await flash(disk, "vfat", "OLD")
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            await flash_scenario(app, blk)


async def test_mount_lost_during_apply(tmp_path):
    with attach("emulated_flash", 64) as disk:
        part = await flash(disk, "vfat", "OLD")
        await mount_lost_scenario(tmp_path, part, emulated_guard(), disk.serial)


async def test_unknown_uuid_matched_by_probe_mounted_manifest(tmp_path):
    with attach("emulated_flash", 64) as disk:
        await flash(disk, "vfat", "OLD")
        await probe_manifest_scenario(tmp_path, disk.serial, emulated_guard())


async def test_eject_unplug_replug(services, tmp_path):
    """Eject leaves nothing mounted; after the unplug the device is gone, after the replug it is matched again
    by its USB serial and has the same content (a new plan changes nothing)."""
    with attach("emulated_flash", 256) as disk:
        await flash(disk, "vfat", "OLD")
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            two = {"rules": [{"id": "t", "criteria": {"is": {"kind": "music"}}, "limit": 2}]}
            req = RegisterRequest(
                name="Eject Stick",
                disk=blk.path,
                rule_set=await own_rules(app, "Eject Stick", two, ["nd"]),
                on_connect="manual",
            )
            cfg = await app.sync.register(req)
            pc = await app.sync.plan(cfg.id)
            job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
            assert job.status == "succeeded", job.error

            await app.sync.eject(cfg.id)
            assert not block_device(disk).all_mountpoints()
            name = blk.name
            unplug(disk)
            await app.sync.on_disk_removed(name)
            assert app.sync.detector.by_device(cfg.id) is None

            replug(disk)
            await app._disk_added(block_device(disk))
            cd = app.sync.detector.by_device(cfg.id)
            assert cd is not None and cd.matched_by == "usb_serial"
            pc = await app.sync.plan(cfg.id)
            assert {x.kind for x in pc.plan.actions} == {ActionKind.KEEP}


async def test_device_io_error_stops_the_sync(services, tmp_path):
    """The device's storage fails mid-sync (its backing store is full: EIO). The sync fails
    at once with the storage hint instead of trying every file; nothing stays mounted; once the storage works
    again the next sync completes."""
    with small_backing_store(48) as store, attach("emulated_flash", 1024, image_dir=store) as disk:
        await flash(disk, "vfat", "OLD")
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            music = {"rules": [{"id": "m", "criteria": {"is": {"kind": "music"}}}]}
            req = RegisterRequest(
                name="Failing Stick",
                disk=blk.path,
                rule_set=await own_rules(app, "Failing Stick", music, ["nd"]),
                on_connect="manual",
            )
            cfg = await app.sync.register(req)
            pc = await app.sync.plan(cfg.id)
            adds = pc.plan.count(ActionKind.ADD)
            job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
            assert job.status == "failed"
            assert "storage" in (job.error or ""), job.error
            assert len((job.result or {}).get("failures", [])) <= 1, (
                "kept trying after the I/O error"
            )

            grow(store, 1024)
            await app.sync.eject(cfg.id)  # the file system may have gone read-only after the errors
            unplug(disk)
            await app.sync.on_disk_removed(blk.name)
            replug(disk)
            repair_vfat(disk.part(1))
            await app._disk_added(block_device(disk))
            pc = await app.sync.plan(cfg.id)
            job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
            assert job.status == "succeeded", job.error
            dev = await app.sync.open_device(cfg.id)
            assert len((await dev.read_manifest()).items) == adds
    # this test's store only: tests on other workers may have one attached right now
    assert not [x for x in sh("losetup", "-a").splitlines() if str(store) in x]


@pytest.mark.slow  # merges the smallest compose book: -m emulated runs it
@pytest.mark.parametrize("merge", [True, False])
async def test_combine_audiobooks_setting(services, tmp_path, merge):
    """A multi-file ABS book goes onto a flash drive as one .m4b with a chapter per file when
    `combine_audiobooks` is on, and as one file per chapter when it is off. (Rockbox takes music only, so the flash
    drive is the disk device where the setting matters.)"""
    from mutagen.mp4 import MP4

    with attach("emulated_flash", 3072) as disk:
        await flash(disk, "vfat", "BOOKS")
        async with running_app(tmp_path, [disk], [abs_config(services)]) as (app, [blk]):
            books: dict[str, list] = {}
            for t in (await (await app.sync.library("abs")).snapshot()).tracks.values():
                if t.kind is MediaKind.AUDIOBOOK:
                    books.setdefault(t.album_id or "", []).append(t)
            multi = [b for b in books.values() if len(b) >= 2]
            assert multi, "a multi-file audiobook is needed"
            # the whole book: a merged book is planned whole, whichever of its files a rule selects; the shortest
            # one is the generated sample book (a 27-part real book took >30 min to re-encode)
            book = min(multi, key=lambda b: sum(t.duration_s or 3600 for t in b))
            assert sum(t.duration_s or 3600 for t in book) < 60, (
                "the 12-second sample book is needed"
            )
            rule = {"id": "b", "criteria": {"is": {"album": book[0].album}}, "kind": ["audiobook"]}
            req = RegisterRequest(
                name="Book Stick",
                disk=blk.path,
                rule_set=await own_rules(app, "Book Stick", {"rules": [rule]}, ["abs"]),
                on_connect="manual",
            )
            cfg = await app.sync.register(req)
            sync = cfg.sync.model_copy(update={"combine_audiobooks": merge})
            cfg = await app.sync.update_device(cfg.model_copy(update={"sync": sync}))
            pc = await app.sync.plan(cfg.id)
            job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
            assert job.status == "succeeded", job.error
            mount = (await app.sync.open_device(cfg.id)).mount
            exts = {".m4b", ".m4a", ".mp3", ".ogg", ".opus", ".flac"}
            audio = [p for p in mount.rglob("*") if p.is_file() and p.suffix.lower() in exts]
            if merge:
                assert len(audio) == 1 and audio[0].suffix == ".m4b", audio
                chapters = MP4(audio[0]).chapters
                assert chapters is not None and len(list(chapters)) == len(book)
            else:
                assert len(audio) == len(book), audio

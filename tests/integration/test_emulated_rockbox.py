"""Acceptance on an emulated Rockbox iPod mini: the scenario of tests/hardware/test_rockbox_acceptance.py -
format keeps the partition table, the firmware partition and /.rockbox; music from the compose stack's Navidrome
as MP3 + .m3u8; no-op re-run; incremental rule change both ways; synthetic playback.log read and rotated. Then the
play log switch and Rename of tests/hardware/test_rockbox_acceptance.py."""

from __future__ import annotations

import pytest

from syncmusic.core.sync import RegisterRequest
from tests.emulated import (
    CLASSIC_LAYOUT,
    EROS_INFO,
    MINI_LAYOUT,
    attach,
    emulated_guard,
    navidrome_config,
    needs_sudo,
    rockbox,
    running_app,
    sh,
)
from tests.factories import own_rules
from tests.hardware.test_probe_mount import rockbox_probe_scenario
from tests.hardware.test_rockbox_acceptance import play_log_rename_scenario, rockbox_scenario

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]


@pytest.mark.slow  # full acceptance scenario: -m emulated runs it
async def test_rockbox_format_sync_incremental_feedback(services, tmp_path):
    with attach("emulated_rockbox", 1024, layout=MINI_LAYOUT) as disk:
        await rockbox(disk, tmp_path / "build")
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            await rockbox_scenario(app, blk)


async def test_play_log_switch_and_rename(services, tmp_path):
    with attach("emulated_rockbox", 256, layout=MINI_LAYOUT) as disk:
        await rockbox(disk, tmp_path / "build")
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            await play_log_rename_scenario(app, blk)


async def test_unknown_rockbox_mini_is_probed_as_rockbox(tmp_path):
    with attach("emulated_rockbox", 96, layout=MINI_LAYOUT) as disk:
        await rockbox(disk, tmp_path / "build")
        await rockbox_probe_scenario(tmp_path, disk.serial, emulated_guard())


async def test_non_apple_rockbox_player(services, tmp_path):
    """Rockbox is not tied to the iPod. A non-Apple player (AIGO EROS Q: one FAT32 partition, no firmware
    partition) with /.rockbox is offered as Rockbox (target from rockbox-info.txt), registers with the Rockbox
    processor template, syncs MP3s, and Wipe (format) keeps the partition table and /.rockbox."""
    with attach("emulated_rockbox_eros", 512, layout=CLASSIC_LAYOUT) as disk:
        await rockbox(disk, tmp_path / "build")
        await rockbox_probe_scenario(tmp_path / "probe", disk.serial, emulated_guard())
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            cd = app.sync.detector.connected[blk.name]
            assert cd.probes[0].type_name == "rockbox" and cd.probes[0].model == "aigoerosq"
            five = {"rules": [{"id": "m", "criteria": {"is": {"kind": "music"}}, "limit": 5}]}
            req = RegisterRequest(
                name="EROS Q",
                disk=blk.path,
                rule_set=await own_rules(app, "EROS Q", five, ["nd"]),
                processor_template_id="builtin-rockbox",
                on_connect="manual",
            )
            cfg = await app.sync.register(req)
            assert cfg.type == "rockbox" and cfg.identity.usb_serial == disk.serial
            table = sh("sfdisk", "--dump", disk.dev)
            cfg = await app.sync.format(cfg.id, confirm="EROS Q")
            assert sh("sfdisk", "--dump", disk.dev) == table
            dev = await app.sync.open_device(cfg.id)
            assert (dev.mount / ".rockbox" / "rockbox-info.txt").read_text() == EROS_INFO
            assert dev.effective_capabilities().has_rtc is False
            # the hosted build's target is a known model
            prof = await app.sync.profile(cfg.id)
            assert prof["known"] and prof["label"].startswith("AIGO EROS Q"), prof
            pc = await app.sync.plan(cfg.id)
            job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
            assert job.status == "succeeded", job.error
            m = await dev.read_manifest()
            assert len(m.items) == 5 and all(e.path.endswith(".mp3") for e in m.items.values())


async def test_playlist_folder_setting(services, tmp_path):
    """Through the real stack: playlists go to the chosen folder; changing it moves them on the next sync."""
    with attach("emulated_rockbox_eros", 512, layout=CLASSIC_LAYOUT) as disk:
        await rockbox(disk, tmp_path / "build")
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            rules = {
                "rules": [
                    {
                        "id": "m",
                        "criteria": {"is": {"kind": "music"}},
                        "limit": 3,
                        "as_playlist": "Три",
                    }
                ]
            }
            req = RegisterRequest(
                name="Lists",
                disk=blk.path,
                type="rockbox",
                rule_set=await own_rules(app, "Lists", rules, ["nd"]),
                on_connect="manual",
                settings={"playlist_dir": "Music/Lists"},
                processor_template_id="builtin-rockbox",  # the emulated tree plays MP3 only
            )
            cfg = await app.sync.register(req)
            for folder in ("Music/Lists", "Playlists"):
                cfg = await app.sync.update_device(
                    cfg.model_copy(update={"settings": {**cfg.settings, "playlist_dir": folder}})
                )
                pc = await app.sync.plan(cfg.id)
                job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
                assert job.status == "succeeded", job.error
                dev = await app.sync.open_device(cfg.id)
                found = sorted(
                    p.relative_to(dev.mount).as_posix() for p in dev.mount.rglob("*.m3u8")
                )
                assert found == [f"{folder}/Три.m3u8"]
                entries = (dev.mount / found[0]).read_text(encoding="utf-8").splitlines()[1:]
                assert len(entries) == 3
                assert all((dev.mount / folder / e).resolve().is_file() for e in entries)
            assert not (dev.mount / "Music" / "Lists").exists()


async def test_rockbox_ipod_registered_as_stock_refuses_format(tmp_path):
    """a Rockbox iPod is offered as Rockbox and, second, as a stock iPod (dual boot). Registered as
    stock, its Format refuses: reformatting the data partition would erase /.rockbox."""
    from syncmusic.core.models import LibraryConfig
    from syncmusic.core.sync import SyncError

    (tmp_path / "music").mkdir()
    lib = LibraryConfig(
        id="music", type="folder", name="Music", settings={"root": str(tmp_path / "music")}
    )
    with attach("emulated_rockbox", 128, layout=MINI_LAYOUT) as disk:
        await rockbox(disk, tmp_path / "build")
        async with running_app(tmp_path, [disk], [lib]) as (app, [blk]):
            cd = app.sync.detector.connected[blk.name]
            assert [p.type_name for p in cd.probes] == ["rockbox", "ipod"]
            cfg = await app.sync.register(
                RegisterRequest(
                    name="Dual", disk=blk.path, type="ipod", processors=[], on_connect="manual"
                )
            )
            assert cfg.type == "ipod"
            with pytest.raises(SyncError, match="Rockbox installed"):
                await app.sync.format(cfg.id, confirm="Dual")
            dev = await app.sync.open_device(cfg.id)
            assert (dev.mount / ".rockbox" / "rockbox-info.txt").is_file()

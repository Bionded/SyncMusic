"""Acceptance on an emulated stock iPod Classic: the scenario of tests/hardware/test_classic_acceptance.py -
format with a name (partition table unchanged), music from Navidrome + podcasts from Audiobookshelf (compose stack),
database re-read with pygpod, no-op re-run, incremental rule change both ways."""

from __future__ import annotations

import pytest

from syncmusic.core.sync import RegisterRequest
from syncmusic.devices.iPod.ipod import ITUNESDB, ITUNESDB_LIMIT
from tests.emulated import (
    CLASSIC_LAYOUT,
    abs_config,
    attach,
    ipod,
    navidrome_config,
    needs_sudo,
    running_app,
)
from tests.factories import own_rules
from tests.hardware.test_classic_acceptance import classic_scenario

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]


@pytest.mark.slow  # full acceptance scenario: -m emulated runs it
async def test_classic_format_sync_incremental(services, tmp_path):
    with attach("emulated_ipod_classic", 2048, layout=CLASSIC_LAYOUT) as disk:
        await ipod(disk, tmp_path / "build", name="Old Classic")
        libraries = [navidrome_config(services), abs_config(services)]
        async with running_app(tmp_path, [disk], libraries) as (app, [blk]):
            await classic_scenario(app, blk)


async def test_itunesdb_size_matches_the_file(services, tmp_path):
    """On a synced emulated Classic: /space reports the iTunesDB's
    real size and the 63 MiB limit; a plan for the same content estimates close to it."""
    with attach("emulated_ipod_classic", 1024, layout=CLASSIC_LAYOUT) as disk:
        await ipod(disk, tmp_path / "build")
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            # 15 tracks size the database as well as all 40 do, in half the time
            music = {"rules": [{"id": "m", "criteria": {"is": {"kind": "music"}}, "limit": 15}]}
            req = RegisterRequest(
                name="Size Classic",
                disk=blk.path,
                rule_set=await own_rules(app, "Size Classic", music, ["nd"]),
                on_connect="manual",
            )
            cfg = await app.sync.register(req)
            pc = await app.sync.plan(cfg.id)
            job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
            assert job.status == "succeeded", job.error
            space = await app.sync.space(cfg.id)
            dev = await app.sync.open_device(cfg.id)
            size = (dev.mount / ITUNESDB).stat().st_size
            assert space["db"] == {"size": size, "limit": ITUNESDB_LIMIT, "level": "ok"}
            db = (await app.sync.plan(cfg.id)).plan.db
            assert db is not None and db.current == size and db.level == "ok"
            assert 0.5 * size < db.estimated < 2 * size + 64 * 1024, (size, db.estimated)


async def test_positions_follow_the_newer_side_both_ways(services, tmp_path):
    """On an emulated Classic with the compose stack's Audiobookshelf: the library's position goes
    to the iPod at the first sync; listened on the iPod and later further on the phone -> the phone's position
    goes to the iPod (nothing sent back); listened on the iPod after the phone -> sent to ABS; a re-sync changes
    nothing on either side."""
    from datetime import datetime, timedelta

    from pygpod import Database as IPodDB

    from syncmusic.core import manifest as manifest_io
    from syncmusic.core.models import MediaKind

    with attach("emulated_ipod_classic", 1024, layout=CLASSIC_LAYOUT) as disk:
        await ipod(disk, tmp_path / "build")
        async with running_app(tmp_path, [disk], [abs_config(services)]) as (app, [blk]):
            abs_lib = await app.sync.library("abs")
            episodes = [
                t
                for t in (await abs_lib.snapshot()).tracks.values()
                if t.kind is MediaKind.PODCAST and (t.duration_s or 0) > 120
            ]
            episode = min(episodes, key=lambda t: t.size_bytes)
            await abs_lib.set_position(episode.ref, 50.0, False)
            app.sync.forget_library("abs")
            rule = {"id": "p", "criteria": {"is": {"title": episode.title}}, "kind": ["podcast"]}
            req = RegisterRequest(
                name="Position Classic",
                disk=blk.path,
                rule_set=await own_rules(app, "Position Classic", {"rules": [rule]}, ["abs"]),
                on_connect="manual",
            )
            cfg = await app.sync.register(req)

            async def sync(**kw):
                app.sync.forget_library("abs")
                pc = await app.sync.plan(cfg.id)
                job = await app.jobs.wait((await app.sync.apply(pc.plan.id, **kw)).id)
                assert job.status == "succeeded", job.error
                return job.result["reverse"] if job.result else {}

            async def bookmark(set_s=None, played=None):
                mount = (await app.sync.open_device(cfg.id)).mount
                key = episode.ref.key()
                dbid = manifest_io.read(mount).items[key].device_ref
                db = IPodDB(str(mount))
                t = next(t for t in db.tracks if str(t.dbid) == dbid)
                if set_s is not None:
                    t.bookmark_time, t.time_played = int(set_s * 1000), played
                    db.save()
                return t.bookmark_time / 1000

            async def abs_position():
                lib = await app.sync.library("abs")
                return (await lib.get_position(episode.ref)).position_s

            await sync(first_sync="keep")
            assert await bookmark() == pytest.approx(50.0, abs=1)
            # on the iPod yesterday, on the phone now: the phone wins
            await bookmark(60.0, datetime.now() - timedelta(days=1))
            await (await app.sync.library("abs")).set_position(episode.ref, 75.0, False)
            rev = await sync()
            assert rev["positions_superseded"] == 1 and rev["positions_pushed"] == 0, rev
            assert await bookmark() == pytest.approx(75.0, abs=1)
            assert await abs_position() == pytest.approx(75.0, abs=1)
            # on the iPod after the phone: the iPod wins
            await bookmark(95.0, datetime.now())
            rev = await sync()
            assert rev["positions_pushed"] == 1, rev
            assert await abs_position() == pytest.approx(95.0, abs=1)
            # no ping-pong
            rev = await sync()
            assert rev["positions_pushed"] == rev["positions_superseded"] == 0, rev
            assert await bookmark() == pytest.approx(95.0, abs=1)
            assert await abs_position() == pytest.approx(95.0, abs=1)

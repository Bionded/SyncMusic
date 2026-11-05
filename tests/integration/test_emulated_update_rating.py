"""Update sync end to end with a real library change: a rating set in the compose
stack's Navidrome (`setRating`, nothing in the media files changes) reaches an emulated stock iPod through a library
refresh → the device is marked → the next schedule tick runs the update sync, which writes the rating into the
iTunesDB and copies no file. (test_emulated_update_sync fakes the change in the manifest instead.)"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from syncmusic.core.sync import RegisterRequest
from tests.emulated import CLASSIC_LAYOUT, attach, ipod, navidrome_config, needs_sudo, running_app
from tests.factories import own_rules

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]

RULES = {"rules": [{"id": "m", "criteria": {"is": {"kind": "music"}}, "limit": 3}]}


def _set_rating(env: dict[str, str], song_id: str, rating: int) -> None:
    params = {
        "u": env["NAVIDROME_USER"],
        "p": env["NAVIDROME_PASSWORD"],
        "v": "1.16.1",
        "c": "sm-test",
        "f": "json",
        "id": song_id,
        "rating": rating,
    }
    body = httpx.get(f"{env['NAVIDROME_URL']}/rest/setRating", params=params, timeout=30).json()
    assert body["subsonic-response"]["status"] == "ok", body


async def test_a_navidrome_rating_reaches_a_connected_ipod_by_update_sync(services, tmp_path):
    with attach("emulated_ipod_classic", 512, layout=CLASSIC_LAYOUT) as disk:
        await ipod(disk, tmp_path / "build")
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            req = RegisterRequest(
                name="Рейтинг",
                disk=blk.path,
                type="ipod",
                rule_set=await own_rules(app, "Рейтинг", RULES, ["nd"]),
                on_connect="manual",
            )
            cfg = await app.sync.register(req)
            pc = await app.sync.plan(cfg.id)
            job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
            assert job.status == "succeeded", job.error
            dev = await app.sync.open_device(cfg.id)
            entry = next(iter((await dev.read_manifest()).items.values()))
            music = dev.mount / "iPod_Control" / "Music"
            files = sorted(p.name for p in music.rglob("*") if p.is_file())
            old = entry.last_rating or 0
            new = 2 if old == 4 else 4

            _set_rating(services, entry.ref.item_id, new)
            try:
                # what a library refresh job does (POST /libraries/nd/refresh): reload + mark the devices
                lib = await app.sync.library("nd")
                await app.sync.snapshots.get(lib, force=True)
                await app.sync.mark_libraries_changed(["nd"])
                jobs = len(app.jobs.jobs())
                assert await app.sync.periodic_updates() == [cfg.id]
                await asyncio.gather(*app.sync._update_tasks)
                new_jobs = app.jobs.jobs()[: len(app.jobs.jobs()) - jobs]
                assert [j.kind for j in new_jobs] == ["update"]
                job = await app.jobs.wait(new_jobs[0].id)
                assert job.status == "succeeded", job.error
                counts = job.result["counts"]
                assert (counts["add"], counts["update_meta"]) == (0, 1), counts

                manifest = await dev.read_manifest()
                assert manifest.items[entry.ref.key()].last_rating == new
                # the iTunesDB holds the same stars as the manifest: no rating difference to report back
                assert not (await dev.read_feedback(manifest)).ratings
                assert sorted(p.name for p in music.rglob("*") if p.is_file()) == files
            finally:
                _set_rating(services, entry.ref.item_id, old)

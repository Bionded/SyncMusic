"""On an emulated iPod with the compose stack's Navidrome: a playlist reordered in Navidrome's web UI
(native moveTrack, which bumps neither `changed` nor `songCount`) reaches the iPod with the next update sync, without
a manual full sync."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from syncmusic.core.sync import RegisterRequest
from tests.emulated import CLASSIC_LAYOUT, attach, ipod, navidrome_config, needs_sudo, running_app
from tests.factories import own_rules

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]

NAME = "sd-test reorder"


async def _move_to_front(env: dict[str, str], pid: str, position: int) -> None:
    """Navidrome's web UI drag: native track ids are 1-based positions."""
    async with httpx.AsyncClient(base_url=env["NAVIDROME_URL"].rstrip("/"), timeout=30) as http:
        for _ in range(8):  # login rate limit
            r = await http.post(
                "/auth/login",
                json={"username": env["NAVIDROME_USER"], "password": env["NAVIDROME_PASSWORD"]},
            )
            if r.status_code != 429:
                break
            await asyncio.sleep(5)
        r.raise_for_status()
        auth = {"x-nd-authorization": f"Bearer {r.json()['token']}"}
        r = await http.put(
            f"/api/playlist/{pid}/tracks/{position}", json={"insert_before": "1"}, headers=auth
        )
        r.raise_for_status()


def _ipod_order(mount: str) -> list[str]:
    from pygpod import Database

    db = Database(mount, apply_play_counts=False)
    pl = next(p for p in db.playlists if p.name == NAME)
    return [str(t.title) for t in pl.tracks]


async def test_web_ui_reorder_reaches_the_ipod_with_the_update(services, tmp_path):
    with attach("emulated_ipod_classic", 512, layout=CLASSIC_LAYOUT) as disk:
        await ipod(disk, tmp_path / "build")
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            lib = await app.sync.library("nd")
            songs = sorted((await lib.snapshot()).tracks)[:3]
            body = await lib.call("createPlaylist", name=NAME, songId=songs)
            pid = str(body["playlist"]["id"])
            try:
                rules = {
                    "rules": [
                        {
                            "id": "p",
                            "criteria": {"inPlaylist": {"id": pid}},
                            "include_playlists": True,
                        }
                    ]
                }
                cfg = await app.sync.register(
                    RegisterRequest(
                        name="Reorder",
                        disk=blk.path,
                        type="ipod",
                        rule_set=await own_rules(app, "Reorder", rules, ["nd"]),
                        on_connect="manual",
                    )
                )
                pc = await app.sync.plan(cfg.id)
                job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
                assert job.status == "succeeded", job.error
                dev = await app.sync.open_device(cfg.id)
                titles = {t.ref.item_id: t.title for t in (await lib.snapshot()).tracks.values()}
                first = await asyncio.to_thread(_ipod_order, str(dev.mount))
                assert first == [titles[s] for s in songs]

                await _move_to_front(services, pid, 3)
                job = await app.jobs.wait((await app.sync.update_sync(cfg.id)).id)
                assert job.status == "succeeded", job.error
                after = await asyncio.to_thread(_ipod_order, str(dev.mount))
                assert after == [titles[songs[2]], titles[songs[0]], titles[songs[1]]]
            finally:
                await lib.call("deletePlaylist", id=pid)

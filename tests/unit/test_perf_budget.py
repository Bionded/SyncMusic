# SPDX-License-Identifier: GPL-3.0-or-later
"""Performance regression guard: the user paths at 2 000 items against generous budgets, so a quadratic path or
work that blocks the event loop fails here instead of on a big library."""

from __future__ import annotations

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.facade import AppFacade
from syncmusic.libraries.subsonic import SubsonicLibrary
from tests.fake_navidrome import FakeNavidrome, song
from tests.media import make_music
from tests.unit.test_api_sync import ALL, _register

pytestmark = pytest.mark.slow

ITEMS = 2000
APPLY_MS_PER_ITEM = 60  # first apply (retag only, tiny files); measured ~20
NOOP_PLAN_S = 2.0  # measured ~0.3
CONTENTS_S = 2.0
LOOP_LAG_MAX_S = 0.45  # a plan that blocks the loop for half a second fails


def _server(tmp_path) -> FakeNavidrome:
    data = make_music(tmp_path / "one.mp3", title="x", artist="x").read_bytes()
    nd = FakeNavidrome(native=False)
    for i in range(ITEMS):
        artist, album = f"Artist {i // 120:03d}", f"Album {i // 12:04d}"
        nd.add(
            song(
                f"s{i}",
                f"Track {i}",
                artist=artist,
                displayArtist=artist,
                displayAlbumArtist=artist,
                album=album,
                albumId=f"al{i // 12}",
                artistId=f"ar{i // 120}",
                track=i % 12 + 1,
                path=f"{artist}/{album}/{i % 12 + 1:02d} Track {i}.mp3",
            )
        )
        nd.songs[f"s{i}"]["_data"] = data
        nd.songs[f"s{i}"]["size"] = len(data)
    return nd


async def _max_lag(coro):  # type: ignore[no-untyped-def]
    """(result, seconds, worst event-loop lag) of awaiting `coro`."""
    lags: list[float] = []

    async def sampler() -> None:
        while True:
            t = time.perf_counter()
            await asyncio.sleep(0.02)
            lags.append(time.perf_counter() - t - 0.02)

    task = asyncio.create_task(sampler())
    t = time.perf_counter()
    try:
        r = await coro
    finally:
        task.cancel()
    return r, time.perf_counter() - t, max(lags, default=0.0)


async def test_user_paths_within_budget(config, tmp_path, monkeypatch):
    monkeypatch.setattr(SubsonicLibrary, "transport", _server(tmp_path), raising=False)
    app = Application(config)
    await app.start()
    try:
        api = create_api(AppFacade(app), manage_lifecycle=False)
        async with AsyncClient(
            transport=ASGITransport(app=api), base_url="http://t", timeout=None
        ) as c:
            r = await c.post(
                "/api/v1/libraries",
                json={
                    "id": "nd",
                    "type": "navidrome",
                    "name": "Navidrome",
                    "settings": {"url": "http://nd.test", "username": "admin", "password": "pw"},
                },
            )
            assert r.status_code == 201, r.text
            procs = [{"type": "retag", "settings": {"replaygain": "off"}}]
            dev = (
                await _register(
                    c,
                    tmp_path / "dev",
                    name="Big",
                    library_ids=["nd"],
                    rule_set=ALL,
                    processors=procs,
                )
            )["id"]
            r, _, lag = await _max_lag(c.post(f"/api/v1/devices/{dev}/plan", json={}))
            assert r.status_code == 200, r.text
            assert lag < LOOP_LAG_MAX_S, f"first plan blocked the loop {lag:.2f} s"

            r = await c.post(f"/api/v1/plans/{r.json()['id']}/apply", json={"first_sync": "keep"})
            assert r.status_code == 202, r.text
            job, dt, _ = await _max_lag(app.jobs.wait(r.json()["id"]))
            assert job.status == "succeeded", job.error
            assert dt * 1000 / ITEMS < APPLY_MS_PER_ITEM, (
                f"first apply {dt * 1000 / ITEMS:.1f} ms/item"
            )

            r, dt, lag = await _max_lag(c.post(f"/api/v1/devices/{dev}/plan", json={}))
            assert r.status_code == 200, r.text
            assert dt < NOOP_PLAN_S, f"no-change plan {dt:.2f} s"
            assert lag < LOOP_LAG_MAX_S, f"no-change plan blocked the loop {lag:.2f} s"

            r, dt, _ = await _max_lag(c.get(f"/api/v1/devices/{dev}/contents"))
            assert r.status_code == 200, r.text
            assert dt < CONTENTS_S, f"contents {dt:.2f} s"
    finally:
        await app.stop()

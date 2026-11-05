"""Reverse sync from virtual devices into the local Navidrome and Audiobookshelf.

A virtual iPod (3 smallest songs from Navidrome + the smallest podcast episode from ABS) and a virtual Rockbox (2
songs) are synced through the API. Then:
- iPod Play Counts with 2 plays per song, 4 stars on one, a bookmark on the episode → the next sync's step 0 raises
  Navidrome's play counts by exactly 2, sets the rating, moves the ABS progress; a re-run adds nothing;
- Navidrome down → the play stays pending (reverse job), and is delivered by the next run once it is back;
- a Rockbox playback.log with one full play per song → +1 each, a re-run adds nothing.
Navidrome's annotation/scrobbles tables and the ABS user's progress are restored afterwards; the test media is
compared before/after. The library-side half of the rating scenario (title change in the library) cannot be made
on a read-only media: tests/unit/test_rating_conflicts.py covers it against the fake Navidrome.
"""

from __future__ import annotations

import struct
import time
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pygpod import Database as IPodDB

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import MediaKind
from syncmusic.facade import AppFacade
from syncmusic.libraries.subsonic import SubsonicLibrary
from tests.contracts.harness import ipod_tree, rockbox_tree
from tests.integration import abs_progress, navidrome_db
from tests.integration.services import media_listing
from tests.integration.test_abs_local import ENV as ABS_ENV
from tests.integration.test_navidrome_local import ENV as ND_ENV

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "NAVIDROME_URL" not in ND_ENV or "ABS_URL" not in ABS_ENV, reason="no local Navidrome/ABS"
    ),
]
MAC_EPOCH = 2082844800


def _listing() -> list[str]:
    return media_listing()


class _Down(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused (test)", request=request)


@pytest.fixture
async def env(services, config):
    if not navidrome_db.available():
        pytest.skip("Navidrome database not reachable in the compose stack")
    app = Application(config)
    await app.start()
    api = create_api(AppFacade(app), manage_lifecycle=False)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
        for body in (
            {
                "id": "nd",
                "type": "navidrome",
                "name": "Navidrome",
                "settings": {
                    "url": ND_ENV["NAVIDROME_URL"],
                    "username": ND_ENV["NAVIDROME_USER"],
                    "password": ND_ENV["NAVIDROME_PASSWORD"],
                    # not needed here; each new library instance logs in, and Navidrome rate-limits
                    # /auth/login (HTTP 429), which would degrade other tests' snapshots
                    "use_native_api": False,
                },
            },
            {
                "id": "abs",
                "type": "audiobookshelf",
                "name": "ABS",
                "settings": {"url": ABS_ENV["ABS_URL"], "api_token": ABS_ENV["ABS_TOKEN"]},
            },
        ):
            r = await c.post("/api/v1/libraries", json=body)
            assert r.status_code == 201, r.text
        nd_state = navidrome_db.save()
        abs_lib = await app.sync.library("abs")
        abs_state = await abs_progress.save(abs_lib)
        try:
            yield app, c
        finally:
            SubsonicLibrary.transport = None
            app.sync.forget_library("nd")
            navidrome_db.restore(nd_state)
            await abs_progress.restore(abs_lib, abs_state)
            assert navidrome_db.save() == nd_state
            assert abs_progress.same(await abs_progress.save(abs_lib), abs_state)
    await app.stop()


async def _job(app, c, path: str, **body) -> dict:
    r = await c.post(path, json=body)
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded", job.error
    return job.result


async def _sync(app, c, device: str, **body) -> dict:
    plan = (await c.post(f"/api/v1/devices/{device}/plan", json={})).json()
    assert "id" in plan, plan
    return await _job(app, c, f"/api/v1/plans/{plan['id']}/apply", **body)


async def _register(c, mount: Path, name: str, type_: str, rules: list[dict], libs: list[str]):
    # a device has templates only: its rules go into a template it links
    tpl = {"name": f"{name} rules", "library_ids": libs, "rules": rules}
    r = await c.post("/api/v1/templates", json=tpl)
    assert r.status_code == 201, r.text
    r = await c.post(
        "/api/v1/devices/register",
        json={
            "name": name,
            "mount_path": str(mount),
            "type": type_,
            "rule_set": {"template_ids": [r.json()["id"]]},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _nd_counts(app) -> dict[str, tuple[int, int | None]]:
    snap = await (await app.sync.library("nd")).snapshot()
    return {i: (t.play_count, t.rating) for i, t in snap.tracks.items()}


def _write_play_counts(mount: Path, per_track, later_s: int = 0) -> None:
    """A Play Counts file as the iPod writes it: one 28-byte entry per track in database order. `later_s`: the
    plays end that much after now (a real play cannot end in the same second as the previous one, and the dedup
    key is the play's time)."""
    db = IPodDB(str(mount), apply_play_counts=False)
    now = int(time.time()) + MAC_EPOCH + later_s
    entries = b""
    for t in db.tracks:
        plays, rating, bookmark_ms = per_track(str(t.dbid))
        entries += struct.pack("<IIIIIII", plays, now if plays else 0, bookmark_ms, rating, 0, 0, 0)
    header = b"mhdp" + struct.pack("<III", 96, 28, len(db.tracks)) + b"\x00" * 80
    (mount / "iPod_Control" / "iTunes" / "Play Counts").write_bytes(header + entries)


async def test_reverse_sync_into_navidrome_and_audiobookshelf(env, tmp_path):
    app, c = env
    data_before = _listing()
    music = {"id": "m", "libraries": ["nd"], "criteria": {"all": []}, "sort": "size", "limit": 3}
    pods = {
        "id": "p",
        "libraries": ["abs"],
        "kind": ["podcast"],
        "criteria": {"all": []},
        "sort": "size",
        "limit": 1,
    }
    ipod = await _register(
        c, ipod_tree(tmp_path / "ipod"), "iPod", "ipod", [music, pods], ["nd", "abs"]
    )
    await _sync(app, c, ipod, first_sync="keep")
    mount = tmp_path / "ipod"
    entries = manifest_io.read(mount).items
    songs = sorted(e.ref.item_id for e in entries.values() if e.ref.library_id == "nd")
    episode = next(e for e in entries.values() if e.kind is MediaKind.PODCAST)
    assert len(songs) == 3
    rated = songs[0]
    by_dbid = {e.device_ref: e for e in entries.values()}

    def counts(dbid: str):
        e = by_dbid[dbid]
        if e.kind is MediaKind.PODCAST:
            return 0, 0, 33_000
        return 2, (80 if e.ref.item_id == rated else 0), 0

    before = await _nd_counts(app)
    _write_play_counts(mount, counts)
    report = await _sync(app, c, ipod)
    rev = report["reverse"]
    assert rev["plays_found"] == 6 and rev["plays_submitted"] == 6 and rev["pending"] == 0, rev
    assert rev["ratings_pushed"] == 1 and rev["positions_pushed"] == 1, rev
    after = await _nd_counts(app)
    for s in songs:
        assert after[s][0] == before[s][0] + 2, s
    assert after[rated][1] == 4
    abs_lib = await app.sync.library("abs")
    pos = (await abs_lib.snapshot()).tracks[episode.ref.item_id].position_s
    assert pos == pytest.approx(33.0, abs=0.5)

    report = await _sync(app, c, ipod)  # nothing new on the device
    assert report["reverse"]["plays_found"] == 0 and (await _nd_counts(app)) == after

    # Navidrome down: the play waits in the database and is delivered by the next run
    by_dbid = {e.device_ref: e for e in manifest_io.read(mount).items.values()}
    last = manifest_io.read(mount).items[f"nd/{songs[1]}"]
    _write_play_counts(
        mount,
        lambda dbid: (1 if by_dbid[dbid].ref == last.ref else 0, 0, 0),  # entries are deltas
        later_s=600,
    )
    SubsonicLibrary.transport = _Down()
    app.sync.forget_library("nd")
    # the update captures the play, keeps it pending and then stops: no plan without the library
    r = await c.post(f"/api/v1/devices/{ipod}/update", json={})
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "failed" and "not available" in str(job.error), job.error
    SubsonicLibrary.transport = None
    app.sync.forget_library("nd")
    rev = (await _job(app, c, f"/api/v1/devices/{ipod}/update"))["reverse"]
    assert rev["plays_found"] == 0 and rev["plays_submitted"] == 1 and rev["pending"] == 0, rev
    after2 = await _nd_counts(app)
    assert after2[songs[1]][0] == after[songs[1]][0] + 1
    assert all(after2[s][0] == after[s][0] for s in songs if s != songs[1])

    # Rockbox: playback.log → plays
    rb_mount = rockbox_tree(tmp_path / "rockbox")
    rb = await _register(c, rb_mount, "Mini", "rockbox", [{**music, "limit": 2}], ["nd"])
    await _sync(app, c, rb, first_sync="keep")
    rb_entries = manifest_io.read(rb_mount).items.values()
    lines = ["# Started Ver. 4.0"] + [
        f"{60_000 * (i + 1)}:{int(e.duration_s * 1000)}:{int(e.duration_s * 1000)}:/{e.path}"
        for i, e in enumerate(rb_entries)
    ]
    (rb_mount / ".rockbox" / "playback.log").write_text("\n".join(lines) + "\n")
    rev = (await _job(app, c, f"/api/v1/devices/{rb}/update"))["reverse"]
    assert rev["plays_found"] == 2 and rev["plays_submitted"] == 2 and rev["pending"] == 0, rev
    after3 = await _nd_counts(app)
    for e in rb_entries:
        assert after3[e.ref.item_id][0] == after2[e.ref.item_id][0] + 1
    rev = (await _job(app, c, f"/api/v1/devices/{rb}/update"))["reverse"]
    assert rev["plays_found"] == 0 and (await _nd_counts(app)) == after3

    assert _listing() == data_before  # the test media is untouched

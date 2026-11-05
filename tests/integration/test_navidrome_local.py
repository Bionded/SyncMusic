"""Navidrome library against the test Navidrome (compose stack, credentials in .env.test), and the same tracks through
Navidrome and the folder library."""

from __future__ import annotations

import asyncio

import pytest

from syncmusic.core.models import ItemRef, LibraryConfig
from syncmusic.libraries.folder import FolderLibrary
from syncmusic.libraries.subsonic import SubsonicLibrary
from tests.integration import services
from tests.integration.services import ENV

# A full fast run -n 3 loads the Pi enough that a media fetch from the shared Navidrome exceeds the 30 s default and
# the track is skipped as failed.
LOADED_TIMEOUT_S = 180.0


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif("NAVIDROME_URL" not in ENV, reason="no Navidrome credentials in .env.test"),
]


@pytest.fixture
async def navidrome(services):
    lib = SubsonicLibrary(
        LibraryConfig(
            id="nd",
            type="navidrome",
            name="Navidrome",
            settings={
                "url": ENV.get("NAVIDROME_URL", ""),
                "username": ENV.get("NAVIDROME_USER", ""),
                "password": ENV.get("NAVIDROME_PASSWORD", ""),
                "timeout_s": LOADED_TIMEOUT_S,
            },
        ),
        None,
    )
    status = await lib.check()
    if not status.ok:
        pytest.skip(f"Navidrome not available: {status.message}")
    yield lib
    await lib.close()


async def test_snapshot_download_lyrics(navidrome):
    snap = await navidrome.snapshot()
    assert snap.tracks and snap.state_token == await navidrome.state_token()
    t = min(snap.tracks.values(), key=lambda t: t.size_bytes)
    size = 0
    async for chunk in await navidrome.open_media(t.ref.item_id):
        size += len(chunk)
    assert size == t.size_bytes
    with_lyrics = [t for t in snap.tracks.values() if t.has_lyrics]
    assert with_lyrics and await navidrome.get_lyrics(with_lyrics[0].ref.item_id)


async def test_track_count_navidrome_vs_folder(navidrome):
    """A music folder has the same tracks through both libraries."""
    folder = FolderLibrary(
        LibraryConfig(
            id="f", type="folder", name="F", settings={"root": str(services.media_root() / "Music")}
        ),
        None,
    )
    nd, fs = await navidrome.snapshot(), await folder.snapshot()
    assert len(nd.tracks) == len(fs.tracks) > 0
    assert {t.path for t in nd.tracks.values()} == {t.path for t in fs.tracks.values()}
    assert sum(t.has_lyrics for t in nd.tracks.values()) == sum(
        t.has_lyrics for t in fs.tracks.values()
    )


async def test_masked_patch_keeps_password_for_check(services, config, tmp_path):
    """the API masks the password; PATCHing the mask back keeps the real one (check still logs in)."""
    from httpx import ASGITransport, AsyncClient

    from syncmusic.api.server import create_api
    from syncmusic.app import Application
    from syncmusic.facade import SECRET_MASK, AppFacade

    app = Application(config)
    await app.start()
    try:
        api = create_api(AppFacade(app), manage_lifecycle=False)
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
            lib = {
                "id": "nd",
                "type": "navidrome",
                "name": "Navidrome",
                "settings": {
                    "url": ENV["NAVIDROME_URL"],
                    "username": ENV["NAVIDROME_USER"],
                    "password": ENV["NAVIDROME_PASSWORD"],
                    "timeout_s": LOADED_TIMEOUT_S,
                },
            }
            r = await c.post("/api/v1/libraries", json=lib)
            assert r.status_code == 201, r.text
            got = (await c.get("/api/v1/libraries/nd")).json()
            assert got["settings"]["password"] == SECRET_MASK
            got["name"] = "Navidrome 2"
            r = await c.patch(
                "/api/v1/libraries/nd", json={"name": got["name"], "settings": got["settings"]}
            )
            assert r.status_code == 200, r.text
            r = await c.post("/api/v1/libraries/nd/check")
            assert r.status_code == 200 and r.json()["ok"], r.text
    finally:
        await app.stop()


async def test_all_template_syncs_every_playlist(navidrome, config, tmp_path):
    """'Sync everything' (built-in All) puts every Navidrome playlist on a flash drive as .m3u8. Two
    playlists are created for the test and deleted afterwards."""
    from httpx import ASGITransport, AsyncClient

    from syncmusic.api.server import create_api
    from syncmusic.app import Application
    from syncmusic.facade import AppFacade

    songs = sorted((await navidrome.snapshot()).tracks)
    names = ["sd-test-188 one", "sd-test-188 two"]
    created = []
    for i, name in enumerate(names):
        body = await navidrome.call("createPlaylist", name=name, songId=songs[i : i + 2 + i])
        created.append(body["playlist"]["id"])
    app = Application(config)
    await app.start()
    try:
        api = create_api(AppFacade(app), manage_lifecycle=False)
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
            lib = {
                "id": "nd",
                "type": "navidrome",
                "name": "Navidrome",
                "settings": {
                    "url": ENV["NAVIDROME_URL"],
                    "username": ENV["NAVIDROME_USER"],
                    "password": ENV["NAVIDROME_PASSWORD"],
                    "timeout_s": LOADED_TIMEOUT_S,
                },
            }
            assert (await c.post("/api/v1/libraries", json=lib)).status_code == 201
            (tmp_path / "stick").mkdir()
            r = await c.post(
                "/api/v1/devices/register",
                json={
                    "name": "Stick",
                    "mount_path": str(tmp_path / "stick"),
                    "type": "flashdrive",
                    "library_ids": ["nd"],
                    "rule_set": {"template_ids": ["default.nd.all"]},
                    "processors": [],
                },
            )
            assert r.status_code == 201, r.text
            plan = (await c.post("/api/v1/devices/stick/plan", json={})).json()
            library_lists = {p.name for p in (await navidrome.snapshot()).playlists.values()}
            assert set(names) <= library_lists
            r = await c.post(f"/api/v1/plans/{plan['id']}/apply", json={"first_sync": "keep"})
            job = await app.jobs.wait(r.json()["id"])
            assert job.status == "succeeded", job.error
            on_device = {p.stem for p in (tmp_path / "stick").rglob("*.m3u8")}
            assert on_device == library_lists
            lines = (tmp_path / "stick" / "Playlists" / f"{names[1]}.m3u8").read_text().splitlines()
            assert len([x for x in lines if x and not x.startswith("#")]) == 3
    finally:
        await app.stop()
        for pid in created:
            await navidrome.call("deletePlaylist", id=pid)


@pytest.fixture
async def smart_playlist(navidrome):
    """A smart playlist on the local Navidrome (its own database; the media stays untouched), removed afterwards."""
    import httpx

    base = ENV["NAVIDROME_URL"].rstrip("/")
    async with httpx.AsyncClient(base_url=base, timeout=30) as http:
        # Navidrome rate-limits logins (AuthRequestLimit); the suite logs in often
        for _ in range(8):
            r = await http.post(
                "/auth/login",
                json={"username": ENV["NAVIDROME_USER"], "password": ENV["NAVIDROME_PASSWORD"]},
            )
            if r.status_code != 429:
                break
            await asyncio.sleep(5)
        r.raise_for_status()
        auth = {"x-nd-authorization": f"Bearer {r.json()['token']}"}
        body = {
            "name": "sd-test smart",
            "rules": {"all": [{"gt": {"playcount": -1}}], "limit": 5},
        }
        r = await http.post("/api/playlist", json=body, headers=auth)
        r.raise_for_status()
        pid = r.json()["id"]
        try:
            yield pid
        finally:
            await http.delete(f"/api/playlist/{pid}", headers=auth)


async def test_snapshot_token_stable_with_a_smart_playlist(navidrome, smart_playlist):
    """Reading a smart playlist makes Navidrome bump its `changed`: the snapshot must still be valid afterwards,
    or every request reloads the whole library (9 s on the user's 12k tracks)."""
    snap = await navidrome.snapshot()
    assert smart_playlist in snap.playlists
    assert snap.state_token == await navidrome.state_token()


async def test_web_ui_reorder_changes_the_token(navidrome, smart_playlist):
    """Navidrome's web UI moves a playlist track (native moveTrack) without bumping `changed` or
    `songCount`; the token must still change, or the automatic update never sees the new order."""
    import httpx

    snap = await navidrome.snapshot()
    ids = list(snap.tracks)[:3]
    pid = str(
        (await navidrome.call("createPlaylist", name="sd-test reorder", songId=ids))["playlist"][
            "id"
        ]
    )
    try:
        before = await navidrome.state_token()
        assert (
            before == await navidrome.state_token()
        )  # stable while nothing changes (smart playlist present)
        async with httpx.AsyncClient(base_url=ENV["NAVIDROME_URL"].rstrip("/"), timeout=30) as http:
            for _ in range(8):  # login rate limit
                r = await http.post(
                    "/auth/login",
                    json={"username": ENV["NAVIDROME_USER"], "password": ENV["NAVIDROME_PASSWORD"]},
                )
                if r.status_code != 429:
                    break
                await asyncio.sleep(5)
            r.raise_for_status()
            auth = {"x-nd-authorization": f"Bearer {r.json()['token']}"}
            # native track ids are 1-based positions: move the third track to the front
            r = await http.put(
                f"/api/playlist/{pid}/tracks/3", json={"insert_before": "1"}, headers=auth
            )
            r.raise_for_status()
        assert await navidrome.state_token() != before
        order = (await navidrome.snapshot()).playlists[pid].track_refs
        assert [t.item_id for t in order] == [ids[2], ids[0], ids[1]]
    finally:
        await navidrome.call("deletePlaylist", id=pid)


async def test_playlist_write_back(navidrome):
    """a playlist is created, read back in order and its songs replaced in a new order; then removed."""
    snap = await navidrome.snapshot()
    refs = sorted(snap.tracks.values(), key=lambda t: t.ref.item_id)[:3]
    a, b, c = (t.ref for t in refs)
    pid = await navidrome.save_playlist("sm-566-test", [a, b])
    try:
        assert pid and await navidrome.get_playlist(pid) == [a, b]
        assert await navidrome.save_playlist("sm-566-test", [c, a], pid) == pid
        assert await navidrome.get_playlist(pid) == [c, a]
    finally:
        await navidrome.call("deletePlaylist", id=pid)
    assert await navidrome.get_playlist(pid) is None


async def test_reverse_playlist_sync_end_to_end(navidrome, config, tmp_path):
    """a Rockbox player (folder device) gets a Navidrome playlist; the user reorders it and makes a
    playlist on the player; the next sync saves both to Navidrome and writes the saved order back."""
    from syncmusic.app import Application
    from syncmusic.core.sync import RegisterRequest
    from tests.contracts.harness import rockbox_tree
    from tests.factories import own_rules

    snap = await navidrome.snapshot()
    small = sorted(snap.tracks.values(), key=lambda t: t.size_bytes)[:3]
    a, b, c = (t.ref.item_id for t in small)
    pid = await navidrome.save_playlist("sm-566 mix", [t.ref for t in small])
    made: str | None = None
    app = Application(config)
    await app.start()
    try:
        await app.store.put_library(navidrome.config)
        mount = rockbox_tree(tmp_path / "rb")
        rules = {
            "rules": [
                {
                    "id": "mix",
                    "name": "Mix",
                    "criteria": {"inPlaylist": {"id": pid}},
                    "include_playlists": True,
                }
            ]
        }
        cfg = await app.sync.register(
            RegisterRequest(
                name="RB",
                mount_path=str(mount),
                type="rockbox",
                rule_set=await own_rules(app, "RB", rules, ["nd"]),
                processors=[],
                on_connect="manual",
            )
        )

        async def sync() -> None:
            pc = await app.sync.plan(cfg.id)
            job = await app.sync.apply(pc.plan.id, first_sync="keep")
            job = await app.jobs.wait(job.id)
            assert job.status == "succeeded", job.error

        await sync()
        dev = await app.sync.open_device(cfg.id)
        m = await dev.read_manifest()
        path = {e.ref.item_id: e.path for e in m.items.values()}
        pl = mount / "Playlists"
        assert (pl / "sm-566 mix.m3u8").is_file()
        # on the player: reversed order; a new playlist with two of the songs
        (pl / "sm-566 mix.m3u8").write_text(
            "#EXTM3U\n" + "".join(f"../{path[i]}\n" for i in (c, b, a))
        )
        (pl / "sm-566 road.m3u8").write_text(
            "#EXTM3U\n" + "".join(f"../{path[i]}\n" for i in (b, a))
        )
        assert m.playlist_sources == {"sm-566 mix": f"nd/{pid}"}
        await sync()
        got = await navidrome.get_playlist(pid)
        assert [r.item_id for r in got or []] == [c, b, a]
        made = (await navidrome.playlists_by_name() or {}).get("sm-566 road")
        assert made and [r.item_id for r in await navidrome.get_playlist(made) or []] == [b, a]
        lines = (pl / "sm-566 mix.m3u8").read_text().splitlines()[1:]
        assert lines == [
            f"../{path[i]}" for i in (c, b, a)
        ]  # the forward sync kept the saved order
        # in the library and selected by no rule: off the player
        assert not (pl / "sm-566 road.m3u8").exists()
        # reordered on both sides: the plan asks, the review's choice decides
        await navidrome.save_playlist(
            "sm-566 mix", [ItemRef(library_id="nd", item_id=i) for i in (b, c, a)], pid
        )
        (pl / "sm-566 mix.m3u8").write_text(
            "#EXTM3U\n" + "".join(f"../{path[i]}\n" for i in (a, c, b))
        )
        pc = await app.sync.plan(cfg.id)
        assert [(x.name, x.kind) for x in pc.plan.playlist_conflicts] == [("sm-566 mix", "reorder")]
        assert [r.item_id for r in await navidrome.get_playlist(pid) or []] == [b, c, a]  # dry
        job = await app.sync.apply(pc.plan.id, playlist_choices={"sm-566 mix": "replace"})
        job = await app.jobs.wait(job.id)
        assert job.status == "succeeded", job.error
        assert [r.item_id for r in await navidrome.get_playlist(pid) or []] == [a, c, b]
    finally:
        await app.stop()
        for p in (pid, made):
            if p:
                await navidrome.call("deletePlaylist", id=p)

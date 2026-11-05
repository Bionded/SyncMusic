"""Navidrome library against the in-memory fake server (tests/fake_navidrome.py)."""

from __future__ import annotations

import pytest

from syncmusic.core.models import LibraryConfig, MediaKind
from syncmusic.core.snapshots import SnapshotCache
from syncmusic.infra.db import Database
from syncmusic.libraries.subsonic import SubsonicLibrary
from tests.fake_navidrome import FakeNavidrome, song
from tests.media import TINY_JPEG


def make(server: FakeNavidrome, **settings) -> SubsonicLibrary:
    lib = SubsonicLibrary(
        LibraryConfig(
            id="nd",
            type="navidrome",
            name="ND",
            settings={"url": "http://nd.test/", "username": "admin", "password": "pw", **settings},
        ),
        None,
    )
    lib.transport = server
    return lib


@pytest.fixture
def server():
    srv = FakeNavidrome()
    srv.add(
        song(
            "a",
            "Група крові",
            userRating=4,
            starred="2026-01-03T00:00:00Z",
            playCount=7,
            played="2026-02-01T12:00:00Z",
            genres=[{"name": "Rock"}, {"name": "Post-Punk"}],
        )
    )
    srv.add(
        song(
            "b",
            "Lossless",
            suffix="m4a",
            contentType="audio/mp4",
            _codec="ALAC",
            _real_path="Real/b.m4a",
        )
    )
    srv.add(song("c", "Episode", type="podcast", album="Show", publishDate="2025-06-10T17:27:28Z"))
    srv.lyrics["a"] = [
        {"lang": "xxx", "synced": False, "line": [{"value": "one"}, {"value": "two"}]},
        {"lang": "rus", "synced": True, "line": [{"start": 1500, "value": "one"}]},
    ]
    srv.playlists["p1"] = {"name": "Мій", "entries": ["a", "c"]}
    srv.playlists["p2"] = {
        "name": "Smart",
        "entries": ["a"],
        "rules": {"all": [{"is": {"loved": True}}], "limit": 5},
    }
    return srv


async def test_snapshot_mapping(server):
    lib = make(server, page_size=2)  # forces paging over 3 songs
    snap = await lib.snapshot()
    assert set(snap.tracks) == {"a", "b", "c"}
    a, b, c = snap.tracks["a"], snap.tracks["b"], snap.tracks["c"]
    assert (a.rating, a.starred, a.play_count, a.last_played.month) == (4, True, 7, 2)
    assert a.genres == ["Rock", "Post-Punk"] and a.has_lyrics and not b.has_lyrics and a.has_cover
    assert b.format == "alac" and b.path == "Real/b.m4a"  # native API refines codec and path
    assert c.kind is MediaKind.PODCAST and c.series == "Show" and c.published.year == 2025
    assert snap.playlists["p1"].track_refs == [a.ref, c.ref] and not snap.playlists["p1"].smart
    assert snap.playlists["p2"].smart
    assert server.calls.count("search3") == 2
    await lib.close()


async def test_native_api_off_or_down_is_soft(server):
    for lib in (make(server, use_native_api=False), make(FakeNavidrome(native=False))):
        if not lib.transport.songs:
            lib.transport.add(song("x", "X", suffix="m4a"))
        snap = await lib.snapshot()
        assert snap.tracks and all(
            not t.has_lyrics and t.format in ("mp3", "aac") for t in snap.tracks.values()
        )
        assert await lib.smart_playlist_rules() == []
        await lib.close()


async def test_smart_playlist_rules(server):
    rules = await make(server).smart_playlist_rules()
    assert [(r.id, r.name, r.limit) for r in rules] == [("nd-p2", "Smart", 5)]
    assert rules[0].criteria == {"all": [{"is": {"loved": True}}]}


async def test_lyrics_prefers_synced(server):
    lib = make(server)
    lyr = await lib.get_lyrics("a")
    assert lyr.synced and lyr.lines == [(1.5, "one")] and lyr.language == "rus"
    server.lyrics["a"] = server.lyrics["a"][:1]
    plain = await lib.get_lyrics("a")
    assert not plain.synced and plain.text == "one\ntwo" and plain.language is None
    assert await lib.get_lyrics("b") is None


async def test_media_and_cover(server):
    lib = make(server)
    await lib.snapshot()
    data = b"".join([c async for c in await lib.open_media("a")])
    assert data == server.songs["a"]["_data"]
    assert await lib.open_cover("a") == TINY_JPEG
    assert await lib.open_cover("missing") is None


async def test_open_media_missing_raises(server):
    # Real Navidrome answers a failed download with HTTP 200 + application/xml (no f=json), see fake.
    lib = make(server)
    with pytest.raises(KeyError, match="download"):
        await lib.open_media("missing")


@pytest.mark.parametrize("native", [True, False])
async def test_snapshot_token_survives_reading_smart_playlists(server, native):
    """Navidrome bumps a smart playlist's `changed` whenever it is read (user's server): the snapshot's
    own playlist reads must not make it stale at once, or every request reloads the whole library."""
    server.smart_reads_bump_changed = True
    lib = make(server, use_native_api=native)
    snap = await lib.snapshot()
    assert snap.state_token == await lib.state_token()


@pytest.mark.parametrize("native", [True, False])
async def test_snapshot_cache_loads_a_library_with_smart_playlists_once(server, native, tmp_path):
    """two requests in a row reuse the cached snapshot instead of reloading the whole library."""
    server.smart_reads_bump_changed = True
    lib = make(server, use_native_api=native)
    db = Database(tmp_path / "sd.db")
    await db.open()
    try:
        cache = SnapshotCache(db)
        first = await cache.get(lib)
        second = await cache.get(lib)
    finally:
        await db.close()
    assert first is second or first == second
    assert server.calls.count("search3") == 1


async def test_token_changes_with_playlists_and_scan(server):
    lib = make(server)
    t0 = await lib.state_token()
    server.playlists["p1"]["changed"] = "later"
    t1 = await lib.state_token()
    server.add(song("d", "New"))
    assert len({t0, t1, await lib.state_token()}) == 3


async def test_token_changes_when_a_playlist_is_reordered_in_place(server):
    # Navidrome's web UI moves a track without bumping `changed` or `songCount`
    lib = make(server)
    t0 = await lib.state_token()
    server.playlists["p1"]["entries"] = ["c", "a"]
    assert await lib.state_token() != t0
    # a smart playlist's order is left out: reading it bumps its `changed`
    t1 = await lib.state_token()
    server.playlists["p2"]["entries"] = ["b"]
    assert await lib.state_token() == t1


class _DeletedAfterListing(FakeNavidrome):
    """p1 is deleted (by another client) between getPlaylists and the getPlaylist of it."""

    def _rest(self, ep, q):
        if ep == "getPlaylist" and q.get("id") == "p1":
            self.playlists.pop("p1", None)
        return super()._rest(ep, q)


async def test_a_playlist_deleted_while_reading_is_left_out():
    server = _DeletedAfterListing()
    server.add(song("a", "Обійми"))
    server.playlists["p1"] = {"name": "Зникне", "entries": ["a"]}
    server.playlists["p2"] = {"name": "Лишиться", "entries": ["a"]}
    token = await make(server).state_token()  # no "playlist not found": the library stays available
    server.playlists["p1"] = {"name": "Зникне", "entries": ["a"]}
    snap = await make(server).snapshot()
    assert [p.name for p in snap.playlists.values()] == ["Лишиться"]
    assert token != await make(server).state_token()


async def test_password_never_in_repr_or_check_message(server):
    lib = make(server, password="hunter2-secret")
    status = await lib.check()
    assert not status.ok and "hunter2" not in status.message
    assert "hunter2" not in repr(lib) and "hunter2" not in repr(lib.settings)


@pytest.mark.timeout(10)
async def test_server_ignoring_offset_does_not_hang(server):
    server.add(song("b", "Two"))
    server.ignore_offset = True
    snap = await make(server, page_size=1).snapshot()
    assert set(snap.tracks) <= {"a", "b"}


async def test_album_cover_change_changes_source_state(server):
    """Navidrome keeps the song row on a cover change; the album's imageHash moves."""
    lib = make(server)
    before = {k: t.source_state for k, t in (await lib.snapshot()).tracks.items()}
    album = server.songs["a"]["albumId"]
    server.album_images[album] = "h1"
    server.last_scan += "x"  # a rescan happened
    lib2 = make(server)
    after = {k: t.source_state for k, t in (await lib2.snapshot()).tracks.items()}
    changed = {k for k in before if before[k] != after[k]}
    same_album = {k for k, s in server.songs.items() if s.get("albumId") == album}
    assert changed == same_album and "a" in changed
    await lib.close()
    await lib2.close()


# --- login rate limit ---------------------------------------------------------------------


@pytest.fixture
def limited(monkeypatch):
    from syncmusic.libraries.subsonic import SubsonicLibrary

    monkeypatch.setattr(SubsonicLibrary, "native_retry_s", (0, 0, 0))


async def test_rate_limited_login_retried_then_native_fields_present(limited):
    server = FakeNavidrome()
    server.add(song("s1", "Зозуля", path="x/Subsonic.mp3", _real_path="Скрябін/Real/Зозуля.mp3"))
    server.refuse_logins = 2
    lib = make(server)
    snap = await lib.snapshot()
    assert server.logins == 3
    assert snap.tracks["s1"].path == "Скрябін/Real/Зозуля.mp3"  # from the native API, not degraded
    await lib.close()


async def test_login_refused_for_good_fails_the_snapshot(limited):
    from syncmusic.libraries.subsonic import NativeUnavailable

    server = FakeNavidrome()
    server.add(song("s1", "Зозуля"))
    server.refuse_logins = 10
    lib = make(server)
    with pytest.raises(NativeUnavailable, match="429"):
        await lib.snapshot()
    assert server.logins == 4  # 1 + 3 retries, not one storm per native call
    await lib.close()


async def test_concurrent_native_calls_log_in_once():
    import asyncio

    server = FakeNavidrome()
    server.add(song("s1", "Зозуля"))
    lib = make(server)
    await asyncio.gather(
        lib._native("song", _start=0, _end=10), lib._native("album", _start=0, _end=10)
    )
    assert server.logins == 1
    await lib.close()

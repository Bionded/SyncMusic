"""Plex library against the in-memory server."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from syncmusic.base.feedback import FeedbackRejected
from syncmusic.base.library import LibraryUnavailable
from syncmusic.core.models import ItemRef, LibraryConfig, PlayEvent
from syncmusic.libraries.plex import PlexLibrary, PlexSettings, _stars
from tests.fake_plex import FakePlex, track


def _lib(server: FakePlex, **settings) -> PlexLibrary:
    cfg = LibraryConfig(
        id="px",
        type="plex",
        name="Plex",
        settings={"url": "plex.test:32400", "token": "token", **settings},
    )
    lib = PlexLibrary(cfg, None)
    lib.transport = server
    return lib


def test_settings_want_a_token():
    with pytest.raises(ValidationError):
        PlexSettings(url="http://plex.test")
    with pytest.raises(ValidationError):
        PlexSettings(url="http://plex.test", token="t", username="x")
    assert PlexSettings(url="plex.test:32400", token="t").url == "http://plex.test:32400"


def test_rating_conversion():
    assert [_stars(v) for v in (None, 0, 1, 2, 5, 8, 10, "x")] == [None, None, 1, 1, 2, 4, 5, None]


async def test_mapping_and_headers():
    server = FakePlex()
    server.add(track("1", "Зозуля", plays=3, user_rating=8, container="flac", size=5000,
                     original_title="Андрій Кузьменко"))  # fmt: skip
    server.add(track("2", "Зірка", index=2, container="m4a", codec="alac", thumb=False))
    server.add(track("3", "Пачка", index=3, container="m4a", codec="aac"))
    server.playlists["p"] = ("Мій плейлист", ["2", "1", "gone"])
    lib = _lib(server)
    status = await lib.check()
    assert status.ok and status.details["version"] == "1.41.0"
    snap = await lib.snapshot()
    t = snap.tracks["1"]
    assert (t.title, t.artist, t.album_artist, t.album, t.album_id, t.track_no, t.disc_no) == (
        "Зозуля", "Андрій Кузьменко", "Скрябін", "Група крові", "al-Група крові", 1, 1,
    )  # fmt: skip
    assert (t.format, t.size_bytes, t.duration_s, t.bitrate_kbps, t.channels, t.year) == (
        "flac", 5000, 200.0, 320, 2, 1988,
    )  # fmt: skip
    assert t.rating == 4 and t.play_count == 3 and not t.starred
    assert t.last_played == datetime.fromtimestamp(1_790_500_000, tz=UTC)
    assert t.date_added == datetime.fromtimestamp(1_790_000_000, tz=UTC)
    assert t.has_cover and t.genres == ["Rock"] and t.path.endswith(".flac")
    assert t.source_state == "5000|1790000100"
    assert (snap.tracks["2"].format, snap.tracks["3"].format) == ("alac", "aac")
    assert snap.tracks["2"].rating is None and snap.tracks["2"].artist == "Скрябін"
    assert [r.item_id for r in snap.playlists["p"].track_refs] == ["2", "1"]
    h = server.headers[-1]
    assert h["accept"] == "application/json" and h["x-plex-product"] == "SyncMusic"
    assert len(h["x-plex-client-identifier"]) == 16
    await lib.close()


async def test_paging_and_section_choice():
    server = FakePlex()
    for i in range(7):
        server.add(track(str(i), f"t{i}", index=i))
    lib = _lib(server, page_size=3, section="music")
    assert len((await lib.snapshot()).tracks) == 7
    assert server.requests.count("GET /library/sections/1/all") == 3
    await lib.close()
    other = _lib(server, section="Jazz")
    status = await other.check()
    assert not status.ok and "no music section 'Jazz'" in status.message
    assert (await other.snapshot()).tracks == {}


async def test_bad_token_and_unreachable():
    status = await _lib(FakePlex(), token="wrong").check()
    assert not status.ok and status.message == "Plex rejected the token"
    assert "wrong" not in status.message


async def test_state_token_moves_on_content_and_playlists():
    server = FakePlex()
    server.add(track("1", "a"))
    server.add(track("2", "b"))
    lib = _lib(server)
    t0 = await lib.state_token()
    a, b = (ItemRef(library_id="px", item_id=x) for x in "12")
    await lib.set_rating(a, 5)
    await lib.submit_plays([PlayEvent(ref=a, played_at=datetime.now(UTC), source_device="d")])
    assert await lib.state_token() == t0  # ratings and plays do not move it
    pid = await lib.save_playlist("x", [a])
    t1 = await lib.state_token()
    assert t1 != t0
    await lib.save_playlist("x", [b], pid)
    assert await lib.state_token() != t1  # an edit of the same size moves it too
    server.add(track("3", "c"))
    assert await lib.state_token() != t1
    await lib.close()


async def test_open_media_fallback_and_errors():
    server = FakePlex()
    server.add(track("1", "a", size=777))
    lib = _lib(server)
    data = b"".join([c async for c in await lib.open_media("1")])  # no snapshot: via the metadata
    assert len(data) == 777 and "GET /library/metadata/1" in server.requests
    with pytest.raises(KeyError):
        await lib.open_media("missing")
    server.fail_downloads_with = 503
    with pytest.raises(LibraryUnavailable):
        await lib.open_media("1")
    server.fail_downloads_with = 404
    with pytest.raises(KeyError):
        await lib.open_media("1")
    await lib.close()


async def test_covers_fall_back_to_the_album():
    server = FakePlex()
    server.add(track("1", "a", thumb=False))
    lib = _lib(server)
    await lib.snapshot()
    assert await lib.open_thumb("1", 64) is not None
    assert server.requests[-1] == "GET /photo/:/transcode"
    assert await lib.open_cover("1") is not None
    assert server.requests[-1] == "GET /library/metadata/al-Група крові/thumb/1"
    assert await lib.open_cover("nope") is None
    assert await lib.request_rescan()
    assert server.requests[-1] == "GET /library/sections/1/refresh"
    await lib.close()


async def test_scrobble_rate_and_playlists():
    server = FakePlex()
    server.add(track("1", "a"))
    server.add(track("2", "b", index=2))
    server.playlists["s"] = ("Smart", ["1"])
    server.smart.add("s")
    lib = _lib(server)
    a, b = (ItemRef(library_id="px", item_id=x) for x in "12")
    at = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    gone = ItemRef(library_id="px", item_id="gone")
    res = await lib.submit_plays(
        [PlayEvent(ref=r, played_at=at, source_device="d") for r in (a, a, gone)]
    )
    assert len(res.accepted) == 2 and len(res.rejected) == 1 and server.scrobbled == ["1", "1"]
    assert await lib.play_count(a) == 2 and await lib.play_count(gone) is None
    await lib.set_rating(b, 3)
    assert server.items["2"]["userRating"] == 6
    await lib.set_rating(b, None)
    assert "userRating" not in server.items["2"]
    with pytest.raises(FeedbackRejected):
        await lib.set_rating(gone, 3)
    with pytest.raises(FeedbackRejected, match="no favourites"):
        await lib.set_starred(a, True)
    pid = await lib.save_playlist("З плеєра", [b, a])
    assert server.playlists[pid] == ("З плеєра", ["2", "1"])
    assert await lib.playlists_by_name() == {"З плеєра": pid}  # the smart one is not offered
    assert await lib.save_playlist("З плеєра", [a], pid) == pid
    assert await lib.get_playlist(pid) == [a]
    assert await lib.get_playlist("nope") is None
    with pytest.raises(FeedbackRejected):
        await lib.save_playlist("x", [ItemRef(library_id="other", item_id="1")])
    await lib.close()

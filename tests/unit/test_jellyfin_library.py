"""Jellyfin library (video only, one way) against the in-memory server."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from syncmusic.base.feedback import FeedbackSink
from syncmusic.core.models import LibraryConfig, MediaKind
from syncmusic.libraries.jellyfin import NOTICE, JellyfinLibrary, JellyfinSettings, _date
from tests.fake_jellyfin import FakeJellyfin, audio, video


def _lib(server: FakeJellyfin, **settings) -> JellyfinLibrary:
    cfg = LibraryConfig(
        id="jf",
        type="jellyfin",
        name="Jellyfin",
        settings={"url": "jf.test:8096", "username": "admin", **settings},
    )
    lib = JellyfinLibrary(cfg, None)
    lib.transport = server
    return lib


def test_settings_want_a_key_or_a_password():
    with pytest.raises(ValidationError, match="API key or the user's password"):
        JellyfinSettings(url="http://jf.test", username="admin")
    assert (
        JellyfinSettings(url="jf.test:8096", username="a", password="x").url
        == "http://jf.test:8096"
    )


def test_dates_with_seven_fraction_digits():
    d = _date("2026-10-30T06:00:00.1234567Z")
    assert d is not None and d.microsecond == 123456 and d.utcoffset().total_seconds() == 0
    assert _date("") is None and _date("nonsense") is None


async def test_password_login_and_video_mapping():
    """movies and episodes only (music is not read), with series / season / episode; no playlists."""
    server = FakeJellyfin()
    server.add(video("m", "Захар Беркут", size=5000, container="mp4"))
    server.add(video("e", "Пілот", series="Серіал", season=2, episode=5))
    server.add(audio("a", "Зозуля"))
    server.playlists["p"] = ("Мій плейлист", ["a"])
    lib = _lib(server, password="pw")
    assert (await lib.check()).ok
    snap = await lib.snapshot()
    assert set(snap.tracks) == {"m", "e"} and not snap.playlists and snap.notice == NOTICE
    m, e = snap.tracks["m"], snap.tracks["e"]
    assert {m.kind, e.kind} == {MediaKind.VIDEO}
    assert (m.title, m.series, m.format, m.size_bytes, m.duration_s, m.codec, m.year) == (
        "Захар Беркут", None, "mp4", 5000, 3000.0, "h264", 2001,
    )  # fmt: skip
    assert m.extra == {"width": 1280, "height": 720} and m.has_cover
    assert (e.series, e.disc_no, e.track_no, e.format) == ("Серіал", 2, 5, "mkv")
    assert e.extra == {"show": "Серіал", "season": 2, "episode": 5, "width": 1280, "height": 720}
    assert "POST /Users/AuthenticateByName" in server.requests
    await lib.close()


def test_one_way_and_beta():
    assert not issubclass(JellyfinLibrary, FeedbackSink)
    assert JellyfinLibrary.capabilities.kinds == {MediaKind.VIDEO} and JellyfinLibrary.beta


async def test_wrong_password_and_unknown_user():
    bad = _lib(FakeJellyfin(), password="nope")
    status = await bad.check()
    assert not status.ok and "user name or password" in status.message
    nobody = _lib(FakeJellyfin(), username="ghost", api_key="key")
    status = await nobody.check()
    assert not status.ok and "no user 'ghost'" in status.message


async def test_download_falls_back_to_a_static_stream():
    server = FakeJellyfin()
    server.add(video("m", "Захар Беркут", size=777))
    server.download_allowed = False
    lib = _lib(server, api_key="key")
    data = b"".join([c async for c in await lib.open_media("m")])
    assert len(data) == 777 and server.requests[-1] == "GET /Videos/m/stream"
    with pytest.raises(KeyError):
        await lib.open_media("missing")
    await lib.close()


async def test_cover_falls_back_to_the_series():
    server = FakeJellyfin()
    server.add(video("e", "Пілот", series="Серіал"))
    lib = _lib(server, api_key="key")
    await lib.snapshot()
    del server.items["e"]  # the episode has no image of its own any more: its series'
    assert await lib.open_thumb("e", 64) is not None
    assert server.requests[-1] == "GET /Items/se-Серіал/Images/Primary"
    assert await lib.request_rescan()
    await lib.close()

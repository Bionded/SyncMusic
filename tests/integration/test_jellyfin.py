"""Jellyfin library against a real Jellyfin 10.10 (compose profile `jellyfin`): the API shapes
the fake server copies - movies and episodes, downloads, logins."""

from __future__ import annotations

import pytest

from syncmusic.core.models import LibraryConfig, MediaKind
from syncmusic.libraries.jellyfin import JellyfinLibrary
from tests.integration import services
from tests.integration.services import ENV

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        "JELLYFIN_URL" not in ENV or not services.docker(), reason="no Jellyfin test setup"
    ),
]


@pytest.fixture(scope="module")
def jellyfin(compose_stack):  # the stack (its network) stays up while Jellyfin runs on it
    services.jellyfin_up()
    yield services.jellyfin_api_key()
    services.jellyfin_down()


def _lib(**settings) -> JellyfinLibrary:
    base = {"url": ENV["JELLYFIN_URL"], "username": ENV["JELLYFIN_USER"], "timeout_s": 120}
    return JellyfinLibrary(
        LibraryConfig(id="jf", type="jellyfin", name="Jellyfin", settings={**base, **settings}),
        None,
    )


async def test_movies_episodes_and_download(jellyfin):
    lib = _lib(api_key=jellyfin)
    assert (await lib.check()).ok
    snap = await lib.snapshot()
    assert not snap.playlists
    by_path = {(t.path or "").rsplit("/", 1)[-1]: t for t in snap.tracks.values()}
    assert sorted(by_path) == sorted(rel.rsplit("/", 1)[-1] for rel, _ in services.JELLYFIN_VIDEOS)
    assert {t.kind for t in snap.tracks.values()} == {MediaKind.VIDEO}
    movie = by_path["Тестовий фільм (2001).mp4"]
    assert movie.series is None and movie.format == "mp4" and movie.codec == "h264"
    assert movie.extra.get("width") == 320 and movie.duration_s and movie.size_bytes > 0
    ep = by_path["Тестовий серіал S01E02.mp4"]
    assert (ep.series, ep.disc_no, ep.track_no) == ("Тестовий серіал", 1, 2)
    assert ep.extra["show"] == "Тестовий серіал" and ep.extra["episode"] == 2
    data = b"".join([c async for c in await lib.open_media(movie.ref.item_id)])
    assert len(data) == movie.size_bytes
    assert await lib.state_token() == snap.state_token
    await lib.close()


async def test_password_login_and_refusals(jellyfin):
    lib = _lib(password=ENV["JELLYFIN_PASSWORD"])
    assert (await lib.check()).ok
    assert len((await lib.snapshot()).tracks) == len(services.JELLYFIN_VIDEOS)
    await lib.close()
    bad = _lib(password="wrong")
    status = await bad.check()
    assert not status.ok and "user name or password" in status.message
    await bad.close()

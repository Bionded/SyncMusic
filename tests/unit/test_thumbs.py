"""Cover thumbnails: scaled + cached per source, a placeholder (204) when there is no cover."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import asyncio
import io

from PIL import Image

from syncmusic.core import thumbs
from tests.fake_navidrome import FakeNavidrome, song
from tests.media import TINY_JPEG, make_music
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401 - fixtures
from tests.unit.test_navidrome_library import make as navidrome


def _png(w=300, h=200) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(out, "PNG")
    return out.getvalue()


async def test_thumbnail_is_scaled_and_cached(tmp_path):
    calls = []

    async def load():
        calls.append(1)
        return _png()

    path = await thumbs.thumbnail(tmp_path, "lib:x", "a", load)
    with Image.open(path) as img:
        assert img.format == "JPEG" and img.size == (thumbs.SIZE, thumbs.SIZE)
    assert await thumbs.thumbnail(tmp_path, "lib:x", "a", load) == path and len(calls) == 1
    # another scope with the same key is another picture
    assert await thumbs.thumbnail(tmp_path, "lib:y", "a", load) != path and len(calls) == 2


async def test_large_thumbnail_is_its_own_picture(tmp_path):
    """The browser's artwork grid asks for LARGE: cached apart from the small one of the same item."""

    async def load():
        return _png(800, 600)

    small = await thumbs.thumbnail(tmp_path, "lib:x", "a", load)
    large = await thumbs.thumbnail(tmp_path, "lib:x", "a", load, thumbs.LARGE)
    assert large != small
    with Image.open(large) as img:
        assert img.size == (thumbs.LARGE, thumbs.LARGE)
    with Image.open(small) as img:
        assert img.size == (thumbs.SIZE, thumbs.SIZE)


async def test_no_cover_is_remembered_for_a_while(tmp_path, monkeypatch):
    calls = []

    async def none():
        calls.append(1)
        return None

    assert await thumbs.thumbnail(tmp_path, "s", "k", none) is None
    assert await thumbs.thumbnail(tmp_path, "s", "k", none) is None and len(calls) == 1
    monkeypatch.setattr(thumbs, "NONE_TTL_S", -1)
    assert await thumbs.thumbnail(tmp_path, "s", "k", none) is None and len(calls) == 2


async def test_broken_or_failing_cover_is_no_cover(tmp_path):
    async def broken():
        return b"not an image"

    async def failing():
        raise OSError("server gone")

    assert await thumbs.thumbnail(tmp_path, "s", "broken", broken) is None
    assert await thumbs.thumbnail(tmp_path, "s", "failing", failing) is None


def test_file_cover_embedded_then_folder_image(tmp_path):
    embedded = make_music(tmp_path / "a" / "x.mp3", title="x", cover=True)
    assert thumbs.file_cover(embedded) == TINY_JPEG
    plain = make_music(tmp_path / "b" / "y.mp3", title="y")
    assert thumbs.file_cover(plain) is None
    (tmp_path / "b" / "Folder.JPG").write_bytes(b"folder")
    assert thumbs.file_cover(plain) == b"folder"


async def test_navidrome_thumb_asks_for_a_scaled_cover():
    srv = FakeNavidrome()
    srv.add(song("a", "A"))
    seen = []
    handle = srv.handle_async_request

    async def spy(request):
        seen.append(dict(request.url.params))
        return await handle(request)

    srv.handle_async_request = spy
    lib = navidrome(srv)
    await lib.snapshot()
    assert await lib.open_thumb("a", 160) == TINY_JPEG
    assert seen[-1]["size"] == "160"
    assert await lib.open_thumb("nope", 160) is None


async def test_library_and_device_thumb_endpoints(env, music, tmp_path):
    """Folder library: embedded cover / cover.jpg → a JPEG with cache headers, none → 404; rows carry `cover_item`
    (tracks, albums, playlists); Player contents rows the same from the device file."""
    app, c = env
    make_music(
        music / "Обкладинка" / "c.mp3",
        title="З обкладинкою",
        artist="Обкладинка",
        album="O",
        cover=True,
    )
    await c.post("/api/v1/libraries/music/refresh")
    for _ in range(100):
        rows = (await c.get("/api/v1/libraries/music/items", params={"page_size": 100})).json()[
            "items"
        ]
        if any(r["title"] == "З обкладинкою" for r in rows):
            break
        await asyncio.sleep(0.05)
    covered = next(r for r in rows if r["title"] == "З обкладинкою")
    plain = next(r for r in rows if r["title"] != "З обкладинкою")
    assert covered["cover_item"] == covered["item_id"] and plain["cover_item"] is None
    r = await c.get("/api/v1/libraries/music/thumb", params={"item": covered["cover_item"]})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert "max-age" in r.headers["cache-control"]
    r = await c.get("/api/v1/libraries/music/thumb", params={"item": plain["item_id"]})
    assert r.status_code == 204
    albums = (await c.get("/api/v1/libraries/music/items", params={"group": "album"})).json()[
        "items"
    ]
    assert next(a for a in albums if a["name"] == "O")["cover_item"] == covered["item_id"]
    # Player contents
    dev = await _register(c, tmp_path / "stick", name="Stick")
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    items = (await c.get(f"/api/v1/devices/{dev['id']}/contents")).json()
    on_dev = next(i for i in items if i["title"] == "З обкладинкою")
    r = await c.get(f"/api/v1/devices/{dev['id']}/contents/thumb", params={"key": on_dev["key"]})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    other = next(i for i in items if i["title"] != "З обкладинкою")
    r = await c.get(f"/api/v1/devices/{dev['id']}/contents/thumb", params={"key": other["key"]})
    assert r.status_code == 204


async def test_one_shared_thumbnail_per_picture(tmp_path):
    """the same cover on two players (and the library) is scaled and stored once."""
    cover = _png(400, 400)
    made = 0

    async def load():
        nonlocal made
        made += 1
        return cover

    a = await thumbs.thumbnail(tmp_path, "dev:one", "x.mp3", load)
    b = await thumbs.thumbnail(tmp_path, "dev:two", "y.mp3", load)
    lib = await thumbs.thumbnail(tmp_path, "lib:nd", "al-1", load)
    assert a and b and lib and len({a, b, lib}) == 3 and made == 3
    shared = list((tmp_path / "thumbs" / thumbs.SHARED).iterdir())
    assert len(shared) == 1 and a.stat().st_ino == b.stat().st_ino == shared[0].stat().st_ino
    other = await thumbs.thumbnail(tmp_path, "dev:one", "x.mp3", load, size=thumbs.LARGE)
    assert other and len(list((tmp_path / "thumbs" / thumbs.SHARED).iterdir())) == 2

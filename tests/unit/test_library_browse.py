"""Library browse API: tracks grouped by album / artist (counts, sizes), one album / artist / series /
playlist, playlists with counts and sizes, search."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from tests.media import make_music
from tests.unit.test_api_sync import env, music  # noqa: F401 - fixtures


async def _get(c, path, **params):
    r = await c.get(f"/api/v1/libraries/music{path}", params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def test_grouped_by_album_and_artist(env, music):
    app, c = env
    tracks = (await _get(c, "/items"))["items"]
    size = {t["title"]: t["size_bytes"] for t in tracks}
    albums = (await _get(c, "/items", group="album"))["items"]
    assert [(a["name"], a["artist"], a["items"], a["field"]) for a in albums] == [
        ("Група крові", "Скрябін", 2, "album_id"),
        ("Гранатовий альбом", "Тартак", 1, "album_id"),
    ]
    assert albums[0]["size_bytes"] == size["Група крові"] + size["Зачини за мною двері"]
    artists = (await _get(c, "/items", group="artist"))["items"]
    assert [(a["name"], a["items"], a["field"]) for a in artists] == [
        ("Скрябін", 2, "artist_id"),
        ("Тартак", 1, "artist_id"),
    ]
    assert (await _get(c, "/items", group="album", query="тартак"))["total"] == 1
    # one album / artist: its exact files
    one = (await _get(c, "/items", album_id=albums[0]["id"]))["items"]
    assert [t["title"] for t in one] == ["Група крові", "Зачини за мною двері"]
    assert [t["title"] for t in (await _get(c, "/items", artist_id=artists[1]["id"]))["items"]] == [
        "Орбіт без цукру"
    ]
    r = await c.get("/api/v1/libraries/music/items", params={"group": "genre"})
    assert r.status_code == 400


async def test_playlists_and_their_tracks(env, music):
    app, c = env
    (music / "Mix.m3u").write_text("Тартак/Орбіт.mp3\nСкрябін/02 Зачини за мною двері.mp3\n")
    (music / "Скрябін.m3u8").write_text("Скрябін/01 Група крові.mp3\n")
    await c.post("/api/v1/libraries/music/refresh")
    pls = await _get(c, "/playlists")
    assert [(p["name"], p["items"]) for p in pls] == [("Mix", 2), ("Скрябін", 1)]
    assert pls[0]["size_bytes"] > 0 and not pls[0]["smart"]
    assert [p["name"] for p in await _get(c, "/playlists", query="скр")] == ["Скрябін"]
    mix = (await _get(c, "/items", playlist_id=pls[0]["id"]))["items"]
    assert [t["title"] for t in mix] == [
        "Орбіт без цукру",
        "Зачини за мною двері",
    ]  # playlist order
    r = await c.get("/api/v1/libraries/music/items", params={"playlist_id": "nope"})
    assert r.status_code == 404


async def test_books_grouped_by_series(env, tmp_path):
    app, c = env
    books = tmp_path / "books"
    for n in (2, 1):
        make_music(books / "Тигролови" / f"0{n}.mp3", title=f"Розділ {n}", artist="Багряний")
    r = await c.post(
        "/api/v1/libraries",
        json={
            "id": "books",
            "type": "folder",
            "name": "B",
            "settings": {"root": str(books), "default_kind": "audiobook"},
        },
    )
    assert r.status_code == 201
    r = await c.get("/api/v1/libraries/books/items", params={"group": "album", "kind": "audiobook"})
    (book,) = r.json()["items"]
    assert (book["name"], book["items"], book["field"], book["id"]) == (
        "Тигролови",
        2,
        "track_id",
        "series:Тигролови",
    )
    r = await c.get("/api/v1/libraries/books/items", params={"series": "Тигролови"})
    assert [t["title"] for t in r.json()["items"]] == ["Розділ 1", "Розділ 2"]  # chapter order

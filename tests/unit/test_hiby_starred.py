"""HiBy hearts ↔ Navidrome starred through a full sync: a heart added on the player stars the song,
a removed one unstars it; never a rating."""

from __future__ import annotations

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.core import manifest as manifest_io
from syncmusic.facade import AppFacade
from syncmusic.libraries.subsonic import SubsonicLibrary
from tests.contracts.harness import hiby_sql, hiby_tree
from tests.fake_navidrome import FakeNavidrome, song
from tests.media import make_music
from tests.unit.test_api_sync import ALL, _apply, _register


@pytest.fixture
def server(tmp_path, monkeypatch):
    nd = FakeNavidrome(native=False)
    for sid, title in (("s1", "Група крові"), ("s2", "Зозуля")):
        path = make_music(tmp_path / "src" / f"{sid}.mp3", title=title, artist="Скрябін")
        s = song(sid, title)
        s["_data"] = path.read_bytes()
        s["size"] = len(s["_data"])
        nd.add(s)
    monkeypatch.setattr(SubsonicLibrary, "transport", nd)
    return nd


@pytest.fixture
async def env(config, server, tmp_path):
    app = Application(config)
    await app.start()
    api = create_api(AppFacade(app), manage_lifecycle=False)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
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
        mount = hiby_tree(tmp_path / "hiby")
        dev = await _register(c, mount, name="HiBy", type="hiby", library_ids=["nd"], rule_set=ALL)
        dev_id = dev["id"] if isinstance(dev, dict) else "hiby"
        await _sync(app, c, dev_id, first_sync="keep")
        yield app, c, mount, dev_id
    await app.stop()


async def _sync(app, c, dev_id, **body):
    plan = (await c.post(f"/api/v1/devices/{dev_id}/plan", json={})).json()
    return await _apply(app, c, plan["id"], **body)


def _heart(mount, item_id: str, on: bool) -> None:
    path = "/mnt/sd_0/" + manifest_io.read(mount).items[f"nd/{item_id}"].path
    sql = (
        f"INSERT INTO FAVOURITES VALUES ('{path}', '', '', 0)"
        if on
        else f"DELETE FROM FAVOURITES WHERE path = '{path}'"
    )
    hiby_sql(mount, "library.db", sql)


async def test_heart_stars_and_unstars(env, server):
    app, c, mount, dev_id = env
    _heart(mount, "s1", True)
    report = await _sync(app, c, dev_id)
    assert report["reverse"]["stars_pushed"] == 1 and report["reverse"]["ratings_pushed"] == 0
    assert server.songs["s1"].get("starred") and "userRating" not in server.songs["s1"]
    report = await _sync(app, c, dev_id)  # settled
    assert report["reverse"]["stars_pushed"] == 0

    _heart(mount, "s1", False)
    report = await _sync(app, c, dev_id)
    assert report["reverse"]["stars_pushed"] == 1 and not server.songs["s1"].get("starred")


def _hearts(mount) -> dict[str, int]:
    con = sqlite3.connect(mount / ".local" / "library.db")
    try:
        rows = con.execute("SELECT path, added_at FROM FAVOURITES").fetchall()
    finally:
        con.close()
    return {p.rsplit("/", 1)[-1]: t for p, t in rows}


async def _refresh(app, c) -> None:
    r = await c.post("/api/v1/libraries/nd/refresh")
    assert r.status_code < 300, r.text


async def test_library_star_becomes_heart_and_unstar_removes_it(env, server):
    """Navidrome star → heart on our file; unstar → gone; nothing is pushed back."""
    app, c, mount, dev_id = env
    server.songs["s2"]["starred"] = "2026-10-28T10:00:00Z"
    await _refresh(app, c)
    report = await _sync(app, c, dev_id)
    name = manifest_io.read(mount).items["nd/s2"].path.rsplit("/", 1)[-1]
    assert name in _hearts(mount)
    report = await _sync(app, c, dev_id)  # our own heart is not sent back as a change
    assert report["reverse"]["stars_pushed"] == 0

    server.songs["s2"].pop("starred")
    await _refresh(app, c)
    report = await _sync(app, c, dev_id)
    assert name not in _hearts(mount)
    assert report["reverse"]["stars_pushed"] == 0


async def test_player_heart_kept_and_pushed_not_removed(env, server):
    """A heart made on the player wins over the library's 'not starred': it is pushed first, then kept."""
    app, c, mount, dev_id = env
    _heart(mount, "s1", True)
    await _sync(app, c, dev_id)
    assert server.songs["s1"].get("starred")
    name = manifest_io.read(mount).items["nd/s1"].path.rsplit("/", 1)[-1]
    # kept; a full sync writes the row anew (the databases are rewritten completely)
    assert set(_hearts(mount)) == {name}

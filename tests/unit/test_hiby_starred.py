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


async def test_reset_favourites_table_unstars_nothing_and_hearts_come_back(
    env, server, monkeypatch
):
    """The player's database lost every heart (a reset): the library keeps its stars and the same sync writes
    the hearts back onto the player."""
    from syncmusic.core import reverse

    monkeypatch.setattr(reverse, "MASS_UNSTAR_MIN", 2)
    app, c, mount, dev_id = env
    for sid in ("s1", "s2"):
        server.songs[sid]["starred"] = "2026-10-28T10:00:00Z"
    await _refresh(app, c)
    await _sync(app, c, dev_id)
    assert len(_hearts(mount)) == 2
    await _sync(app, c, dev_id)  # settled: the seen set holds both

    hiby_sql(mount, "library.db", "DELETE FROM FAVOURITES")
    report = await _sync(app, c, dev_id)
    assert report["reverse"]["stars_pushed"] == 0
    assert any("favourites were reset" in w for w in report["reverse"]["warnings"])
    assert all(server.songs[sid].get("starred") for sid in ("s1", "s2"))
    assert len(_hearts(mount)) == 2
    report = await _sync(app, c, dev_id)
    assert report["reverse"]["stars_pushed"] == 0 and len(_hearts(mount)) == 2


async def test_held_favourite_removals_can_be_sent_on_purpose(env, server, monkeypatch):
    """The user confirms that the vanished hearts were removed on purpose: the next sync unstars them."""
    from syncmusic.core import reverse

    monkeypatch.setattr(reverse, "MASS_UNSTAR_MIN", 2)
    app, c, mount, dev_id = env
    for sid in ("s1", "s2"):
        server.songs[sid]["starred"] = "2026-10-28T10:00:00Z"
    await _refresh(app, c)
    await _sync(app, c, dev_id)
    await _sync(app, c, dev_id)
    hiby_sql(mount, "library.db", "DELETE FROM FAVOURITES")
    report = await _sync(app, c, dev_id)
    assert sorted(report["reverse"]["held_unstars"]) == ["nd/s1", "nd/s2"]
    url = f"/api/v1/devices/{dev_id}/sync-reports/{report['id']}/send-held-unstars"
    r = await c.post(url)
    assert r.status_code == 200 and r.json() == {"queued": 2}
    assert (await c.post(url)).json() == {"queued": 0}  # the report forgot them
    assert (
        await c.post(f"/api/v1/devices/{dev_id}/sync-reports/nope/send-held-unstars")
    ).status_code == 404
    await _sync(app, c, dev_id)
    assert not any(server.songs[sid].get("starred") for sid in ("s1", "s2"))


async def test_listening_changes_of_a_sync_can_be_undone(env, server):
    """A sync that unstarred favourites in the library: undoing its changes stars them again."""
    app, c, mount, dev_id = env
    server.songs["s1"]["starred"] = "2026-10-28T10:00:00Z"
    await _refresh(app, c)
    await _sync(app, c, dev_id)
    await _sync(app, c, dev_id)
    hiby_sql(mount, "library.db", "DELETE FROM FAVOURITES")
    report = await _sync(app, c, dev_id)
    assert not server.songs["s1"].get("starred")
    assert report["reverse"]["changes"] == [
        {"kind": "star", "ref": "nd/s1", "value": False, "previous": True}
    ]
    url = f"/api/v1/devices/{dev_id}/sync-reports/{report['id']}/undo-listening"
    assert (await c.post(url)).json() == {"queued": 1}
    assert (await c.post(url)).json() == {"queued": 0}  # the report forgot them
    assert (
        await c.post(f"/api/v1/devices/{dev_id}/sync-reports/nope/undo-listening")
    ).status_code == 404
    await _sync(app, c, dev_id)
    assert server.songs["s1"].get("starred")
    await _refresh(app, c)
    await _sync(app, c, dev_id)  # the player follows the library again
    assert len(_hearts(mount)) == 1

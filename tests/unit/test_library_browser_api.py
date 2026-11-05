"""Library browser backend: "On devices" from the manifests, sort, tab counts, adding to a
template, and the Undo of an add (to a device or a template)."""

from __future__ import annotations

from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401 (fixtures)


async def _ids(c, **params):
    return [
        i["item_id"]
        for i in (await c.get("/api/v1/libraries/music/items", params=params)).json()["items"]
    ]


async def test_on_devices_tab_counts_and_sort(env, tmp_path):  # noqa: F811
    app, c = env
    await _register(c, tmp_path / "flash", rule_set={"rules": []}, library_ids=[])
    res = (await c.get("/api/v1/libraries/music/items")).json()
    assert res["tab_counts"] == {
        "songs": 3,
        "albums": 2,
        "artists": 2,
        "playlists": 0,
        "books": 0,
        "podcasts": 0,
        "videos": 0,
    }
    assert all(i["on_devices"] == [] for i in res["items"])

    orbit = await _ids(c, query="орбіт")
    items = (await c.get("/api/v1/libraries/music/items")).json()["items"]
    krovi = [i["item_id"] for i in items if i["title"] == "Група крові"]  # one of its album's two
    await c.post(
        "/api/v1/devices/my-flash/manual-selection",
        json={"library_id": "music", "ids": orbit + krovi},
    )
    plan = (await c.post("/api/v1/devices/my-flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")

    rows = {i["title"]: i for i in (await c.get("/api/v1/libraries/music/items")).json()["items"]}
    assert rows["Орбіт без цукру"]["on_devices"] == ["My Flash"]
    assert rows["Зачини за мною двері"]["on_devices"] == []
    albums = {
        g["name"]: g
        for g in (await c.get("/api/v1/libraries/music/items", params={"group": "album"})).json()[
            "items"
        ]
    }
    assert albums["Гранатовий альбом"]["on_devices"] == ["My Flash"]
    assert albums["Гранатовий альбом"]["on_devices_partly"] == []
    # an album partly on a device: no full chip, a "1/2" one
    assert albums["Група крові"]["on_devices"] == []
    assert albums["Група крові"]["on_devices_partly"] == [{"name": "My Flash", "items": 1}]

    # recorded in the database: a restarted app (no manifest read yet) still knows it
    app.sync._device_items.clear()
    assert await app.store.device_items() == {
        "my-flash": {f"music/{orbit[0]}", f"music/{krovi[0]}"}
    }
    # a deleted device leaves no chips behind
    assert (await c.delete("/api/v1/devices/my-flash")).status_code == 204
    assert await app.store.device_items() == {}

    # every sort the browser offers is accepted; an unknown one is a 400
    for sort in ("artist", "added", "rating", "year", "title", "show"):
        r = await c.get("/api/v1/libraries/music/items", params={"sort": sort})
        assert r.status_code == 200 and r.json()["total"] == 3
    assert (
        await c.get("/api/v1/libraries/music/items", params={"sort": "nope"})
    ).status_code == 400
    titles = [
        i["title"]
        for i in (await c.get("/api/v1/libraries/music/items", params={"sort": "title"})).json()[
            "items"
        ]
    ]
    assert titles == sorted(titles, key=str.casefold)  # A–Z


async def test_add_to_template_and_undo(env, tmp_path):  # noqa: F811
    app, c = env
    await _register(c, tmp_path / "flash", rule_set={"rules": []}, library_ids=[])
    tpl = (
        await c.post("/api/v1/templates", json={"name": "Road trip", "library_ids": ["music"]})
    ).json()
    orbit = await _ids(c, query="орбіт")
    r = await c.post(
        f"/api/v1/templates/{tpl['id']}/manual-selection",
        json={"library_id": "music", "ids": orbit},
    )
    assert r.status_code == 200, r.text
    assert r.json()["added"] == orbit
    assert r.json()["rules"][0]["criteria"] == {"any": [{"in": {"track_id": orbit}}]}
    # added again: nothing new, so an Undo of this one removes nothing
    again = await c.post(
        f"/api/v1/templates/{tpl['id']}/manual-selection",
        json={"library_id": "music", "ids": orbit},
    )
    assert again.json()["added"] == []
    undo = await c.post(
        f"/api/v1/templates/{tpl['id']}/manual-selection",
        json={"library_id": "music", "ids": orbit, "remove": True},
    )
    # the Undo leaves the template as it was before the add: no empty Manual selection rule
    assert undo.status_code == 200 and undo.json()["rules"] == []

    # a template of another library only is refused
    (tmp_path / "other").mkdir()
    lib = {
        "id": "other",
        "type": "folder",
        "name": "Other",
        "settings": {"root": str(tmp_path / "other")},
    }
    assert (await c.post("/api/v1/libraries", json=lib)).status_code == 201
    other = await c.post("/api/v1/templates", json={"name": "Other", "library_ids": ["other"]})
    assert other.status_code in (200, 201), other.text
    r = await c.post(
        f"/api/v1/templates/{other.json()['id']}/manual-selection",
        json={"library_id": "music", "ids": orbit},
    )
    assert r.status_code == 400 and "does not draw from" in r.json()["message"]
    r = await c.post(
        "/api/v1/templates/nope/manual-selection", json={"library_id": "music", "ids": orbit}
    )
    assert r.status_code == 404

    # the device's Undo: only the ids that were new go
    kino = await _ids(c, query="скрябін")
    r = await c.post(
        "/api/v1/devices/my-flash/manual-selection", json={"library_id": "music", "ids": kino[:1]}
    )
    assert r.json()["added"] == kino[:1]
    r = await c.post(
        "/api/v1/devices/my-flash/manual-selection", json={"library_id": "music", "ids": kino}
    )
    assert r.json()["added"] == kino[1:]
    r = await c.post(
        "/api/v1/devices/my-flash/manual-selection",
        json={"library_id": "music", "ids": r.json()["added"], "remove": True},
    )
    own = (await c.get("/api/v1/templates/own-my-flash")).json()
    assert own["rules"][0]["criteria"] == {"any": [{"in": {"track_id": kino[:1]}}]}


async def test_undo_keeps_a_default_template_generated(env, tmp_path):  # noqa: F811
    """Add to an untouched library default, then Undo: the same rules and not 'edited'; ids of a
    Manual selection that was there before the add stay."""
    _, c = env
    before = next(
        t for t in (await c.get("/api/v1/templates")).json() if t.get("default_of") == "music"
    )
    assert before["edited"] is False
    path = f"/api/v1/templates/{before['id']}/manual-selection"
    orbit = await _ids(c, query="орбіт")
    kino = await _ids(c, query="скрябін")
    added = (await c.post(path, json={"library_id": "music", "ids": orbit})).json()
    assert added["edited"] is True and added["added"] == orbit
    undo = (await c.post(path, json={"library_id": "music", "ids": orbit, "remove": True})).json()
    assert undo["edited"] is False and undo["rules"] == before["rules"]
    after = (await c.get(f"/api/v1/templates/{before['id']}")).json()
    assert after["edited"] is False and after["rules"] == before["rules"]

    # a selection made earlier keeps its ids; only the Undo's ids go
    await c.post(path, json={"library_id": "music", "ids": kino[:1]})
    await c.post(path, json={"library_id": "music", "ids": orbit})
    undo = (await c.post(path, json={"library_id": "music", "ids": orbit, "remove": True})).json()
    manual = next(r for r in undo["rules"] if r["id"] == "manual-selection")
    assert manual["criteria"] == {"any": [{"in": {"track_id": kino[:1]}}]}
    assert undo["edited"] is True


def test_show_sort_orders_episodes_then_movies():
    """the video tab's "show" sort - shows A–Z by season and episode, movies (no show) last."""
    from syncmusic.core.models import ItemRef, MediaKind, Track
    from syncmusic.facade import _ASC_SORTS

    def v(i, title, show=None, season=None, episode=None):
        extra = {k: x for k, x in {"season": season, "episode": episode}.items() if x is not None}
        ref = ItemRef(library_id="v", item_id=i)
        return Track(ref=ref, kind=MediaKind.VIDEO, title=title, series=show, extra=extra)

    rows = [
        v("1", "Фільм"),
        v("2", "b", "Мультики", 1, 2),
        v("3", "a", "Мультики", 1, 1),
        v("4", "z", "Мультики", 2, 1),
    ]
    assert [t.ref.item_id for t in sorted(rows, key=_ASC_SORTS["show"])] == ["3", "2", "4", "1"]

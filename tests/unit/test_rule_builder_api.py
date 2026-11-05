"""Rule builder API: field catalogue, autocomplete values from the index, text conversion, rule test
without a device (matches + size), and an in-use template that cannot be deleted without detaching."""
# ruff: noqa: F811 - pytest fixtures imported from test_api_sync

from __future__ import annotations

from tests.unit.test_api_sync import _register, _removable_true, env, music  # noqa: F401

ALL = {"rules": [{"id": "all", "criteria": {"all": []}}]}


async def test_fields_and_operators(env):
    app, c = env
    data = (await c.get("/api/v1/rules/fields")).json()
    types = {f["name"]: f["type"] for f in data["fields"]}
    assert (
        types["artist"] in ("string", "list")
        and types["year"] == "number"
        and types["loved"] == "bool"
    )
    assert "track_id" not in types
    assert (
        "inTheLast" in data["operators"]["date"] and "inPlaylist" in data["operators"]["playlist"]
    )


async def test_values_for_autocomplete(env):
    app, c = env
    r = await c.get("/api/v1/rules/values", params={"field": "artist", "q": "сКр"})
    assert r.status_code == 200, r.text
    assert r.json()[0] == {"value": "Скрябін", "count": 2}
    assert [
        v["value"] for v in (await c.get("/api/v1/rules/values", params={"field": "album"})).json()
    ] == [
        "Група крові",
        "Гранатовий альбом",
    ]
    libs = (await c.get("/api/v1/rules/values", params={"field": "library_id", "q": "mus"})).json()
    assert libs == [{"value": "music", "count": 1, "label": "Music"}]
    r = await c.get("/api/v1/rules/values", params={"field": "year"})
    assert r.status_code == 400  # numbers are typed, not suggested


async def test_text_roundtrip_and_test_without_device(env):
    app, c = env
    rs = {
        "rules": [
            {
                "id": "skryabin",
                "name": "Скрябін",
                "criteria": {"all": [{"contains": {"artist": "Скрябін"}}]},
            }
        ]
    }
    text = (await c.post("/api/v1/rules/text", json={"rule_set": rs})).json()["text"]
    assert "Скрябін" in text and "contains" in text
    back = (await c.post("/api/v1/rules/validate", json={"text": text})).json()
    assert back["ok"] and back["rule_set"]["rules"][0]["criteria"] == rs["rules"][0]["criteria"]
    res = (await c.post("/api/v1/rules/test", json={"rule_set": rs})).json()
    assert res["items"] == 2 and res["bytes"] > 0 and res["per_rule"][0]["rule_id"] == "skryabin"
    res = (
        await c.post("/api/v1/rules/test", json={"rule_set": ALL, "library_ids": ["music"]})
    ).json()
    assert res["items"] == 3


async def test_in_use_template_needs_detach(env, monkeypatch, tmp_path):
    app, c = env
    monkeypatch.setattr(type(app.sync), "_removable", _removable_true)
    tpl = (await c.post("/api/v1/templates", json={"name": "Skryabin", **ALL})).json()
    await _register(c, tmp_path / "flash", rule_set={"template_ids": [tpl["id"]]})
    r = await c.delete(f"/api/v1/templates/{tpl['id']}")
    assert r.status_code == 409 and "detach" in r.json()["message"]
    r = await c.delete(f"/api/v1/templates/{tpl['id']}", params={"detach": "true"})
    assert r.status_code == 204
    dev = (await c.get("/api/v1/devices/my-flash")).json()["config"]
    assert dev["rule_set"]["template_ids"] == [] and dev["rule_set"]["rules"] == []  # unlinked


async def test_rule_test_lists_each_warning_once(env):
    """validation and evaluation both reported the same parser warning."""
    app, c = env
    for cond in ({"contains": {"fooBar": "a"}}, {"contains": {"artist": ""}}):
        rs = {
            "rules": [{"id": "x", "criteria": {"all": [cond]}}],
            "excludes": [{"id": "ex", "criteria": {"all": [cond]}}],
        }
        res = (await c.post("/api/v1/rules/test", json={"rule_set": rs})).json()
        warnings = res["warnings"]
        assert len(warnings) == 2, warnings
        assert sum(w.startswith("rule x: ") for w in warnings) == 1
        assert sum(w.startswith("exclude ex: ") for w in warnings) == 1
    rs = {"rules": [{"id": "x", "criteria": {"all": []}}], "priority": ["nope"]}
    res = (await c.post("/api/v1/rules/test", json={"rule_set": rs})).json()
    assert res["warnings"] == ["priority lists unknown rule id 'nope'"]


async def test_values_do_not_wait_for_dead_libraries(env, monkeypatch):
    """libraries whose server never answers do not hold back the suggestions of the others."""
    import asyncio
    import time

    from syncmusic import facade

    app, c = env
    held: list[asyncio.StreamWriter] = []

    async def hold(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        held.append(w)

    srv = await asyncio.start_server(hold, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
    monkeypatch.setattr(facade, "SUGGEST_WAIT_S", 0.3)
    try:
        for lid, typ, settings in [
            ("nd_to", "navidrome", {"url": url, "username": "u", "password": "p", "timeout_s": 3}),
            ("abs_to", "audiobookshelf", {"url": url, "api_token": "t", "timeout_s": 3}),
        ]:
            r = await c.post(
                "/api/v1/libraries",
                json={"id": lid, "type": typ, "name": lid, "settings": settings},
            )
            assert r.status_code == 201, r.text
        for _ in range(3):  # the first request (loads start) and repeats (loads still running)
            t0 = time.monotonic()
            r = await c.get("/api/v1/rules/values", params={"field": "artist", "q": "сКр"})
            assert r.status_code == 200, r.text
            assert r.json()[0] == {"value": "Скрябін", "count": 2}
            assert time.monotonic() - t0 < 1.5
        await asyncio.sleep(3.2)  # the background loads have failed: not retried at once
        t0 = time.monotonic()
        r = await c.get("/api/v1/rules/values", params={"field": "artist", "q": "сКр"})
        assert r.json()[0] == {"value": "Скрябін", "count": 2}
        assert time.monotonic() - t0 < 0.2
    finally:
        for w in held:
            w.close()
        srv.close()


async def test_values_retry_a_library_at_once_after_its_settings_change(env, tmp_path):
    """a library whose suggestion load failed is used again right after a PATCH fixes its settings."""
    from tests.media import make_music

    app, c = env
    more = tmp_path / "more"
    make_music(
        more / "Скрябін" / "03 Зозуля.mp3", title="Зозуля", artist="Скрябін", album="Чорний альбом"
    )
    r = await c.post(
        "/api/v1/libraries",
        json={"id": "more", "type": "folder", "name": "More", "settings": {"root": str(more)}},
    )
    assert r.status_code == 201, r.text
    moved = more.rename(tmp_path / "moved")  # the folder vanishes: the suggestion load fails
    r = await c.get("/api/v1/rules/values", params={"field": "artist", "q": "сКр"})
    assert r.json()[0] == {"value": "Скрябін", "count": 2}
    r = await c.patch("/api/v1/libraries/more", json={"settings": {"root": str(moved)}})
    assert r.status_code == 200, r.text
    r = await c.get("/api/v1/rules/values", params={"field": "artist", "q": "сКр"})
    assert r.json()[0] == {"value": "Скрябін", "count": 3}


async def test_album_values_name_their_album_artist(env):
    """The picker shows each album with its (most frequent) album artist and finds it by the artist."""
    app, c = env
    albums = (await c.get("/api/v1/rules/values", params={"field": "album", "limit": 200})).json()
    assert {v["value"]: v.get("label") for v in albums} == {
        "Група крові": "Скрябін",
        "Гранатовий альбом": "Тартак",
    }
    found = (await c.get("/api/v1/rules/values", params={"field": "album", "q": "тар"})).json()
    assert [v["value"] for v in found] == ["Гранатовий альбом"]
    artists = (await c.get("/api/v1/rules/values", params={"field": "artist"})).json()
    assert all("label" not in v for v in artists)

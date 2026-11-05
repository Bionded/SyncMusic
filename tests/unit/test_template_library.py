"""a template draws from one, several or all libraries (its rules select only from them), conditions
nest as all/any groups, stored one-library templates keep their library, and autocomplete starts after more than 2
characters, case-insensitive, anywhere in the value, from the given libraries only."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from syncmusic.app import Application
from syncmusic.core.models import ItemRef, Playlist, RuleSet, RuleTemplate
from syncmusic.core.rules import evaluate_ruleset
from tests.factories import snapshot, track
from tests.unit.test_api_sync import _register, env, music  # noqa: F401 - fixtures

ALL = [{"id": "r", "criteria": {"all": []}}]


def _libs():
    nd = [
        track("1", "nd", artist="John Denver"),
        track("2", "nd", artist="Pigeon John"),
        track("3", "nd", artist="Hall & Oates"),
        track("4", "nd", artist="Skryabin"),
    ]
    pls = [
        Playlist(
            ref=ItemRef(library_id="nd", item_id="px"),
            name="XXX",
            track_refs=[nd[0].ref, nd[2].ref],
        ),
        Playlist(ref=ItemRef(library_id="nd", item_id="pz"), name="ZZZ", track_refs=[nd[3].ref]),
    ]
    return {
        "nd": snapshot(nd, "nd", playlists=pls),
        "folder": snapshot([track("1", "folder", artist="John Denver")], "folder"),
    }


def test_a_bound_template_selects_only_from_its_library():
    tpl = RuleTemplate.model_validate({"id": "t", "name": "T", "library_id": "nd", "rules": ALL})
    assert tpl.library_ids == ["nd"]  # the one-library form of
    sel = evaluate_ruleset(RuleSet(template_ids=["t"]), _libs(), {"t": tpl})
    assert sorted(sel.items) == ["nd/1", "nd/2", "nd/3", "nd/4"]
    every = tpl.model_copy(update={"library_ids": []})  # all libraries
    assert len(evaluate_ruleset(RuleSet(template_ids=["t"]), _libs(), {"t": every}).items) == 5
    # several templates of different libraries on one device
    other = RuleTemplate.model_validate(
        {"id": "o", "name": "O", "library_ids": ["folder"], "rules": ALL}
    )
    sel = evaluate_ruleset(RuleSet(template_ids=["t", "o"]), _libs(), {"t": tpl, "o": other})
    assert len(sel.items) == 5


def test_a_template_of_several_libraries():
    """one, two or all libraries; a rule's own library list is narrowed to the template's."""
    libs = _libs()
    libs["third"] = snapshot([track("9", "third", artist="Skryabin")], "third")

    def items(library_ids, rules=ALL):
        tpl = RuleTemplate.model_validate(
            {"id": "t", "name": "T", "library_ids": library_ids, "rules": rules}
        )
        return sorted(evaluate_ruleset(RuleSet(template_ids=["t"]), libs, {"t": tpl}).items)

    assert items(["folder"]) == ["folder/1"]
    assert items(["nd", "third"]) == ["nd/1", "nd/2", "nd/3", "nd/4", "third/9"]
    assert len(items([])) == 6
    narrowed = [{"id": "r", "libraries": ["third", "folder"], "criteria": {"all": []}}]
    assert items(["nd", "third"], narrowed) == ["third/9"]
    kino = [{"id": "r", "criteria": {"all": [{"is": {"artist": "Skryabin"}}]}}]
    assert items(["folder", "third"], kino) == ["third/9"]
    assert items([], kino) == ["nd/4", "third/9"]


def test_nested_groups():
    """(in playlist XXX and artist contains John) or in playlist ZZZ."""
    criteria = {
        "any": [
            {"all": [{"inPlaylist": {"name": "XXX"}}, {"contains": {"artist": "john"}}]},
            {"inPlaylist": {"name": "ZZZ"}},
        ]
    }
    tpl = RuleTemplate.model_validate(
        {"id": "t", "name": "T", "library_id": "nd", "rules": [{"id": "r", "criteria": criteria}]}
    )
    sel = evaluate_ruleset(RuleSet(template_ids=["t"]), _libs(), {"t": tpl})
    assert sorted(sel.items) == ["nd/1", "nd/4"]


async def test_stored_one_library_templates_keep_their_library(config, music):
    """A template saved with `library_id` loads with that library; one without stays 'all'."""
    import json

    app = Application(config)
    await app.start()
    try:
        for tid, lib in (("old", "b"), ("free", None)):
            data = {"id": tid, "name": tid, "library_id": lib, "rules": ALL}
            await app.store.db.execute(
                "INSERT INTO templates (id, name, data, updated_at) VALUES (?, ?, ?, ?)",
                (tid, tid, json.dumps(data), "2026-10-20T00:00:00Z"),
            )
    finally:
        await app.stop()
    app = Application(config)
    await app.start()
    try:
        tpls = await app.store.templates()
        assert tpls["old"].library_ids == ["b"]
        assert tpls["free"].library_ids == []  # all libraries
    finally:
        await app.stop()


async def test_template_api_library(env, tmp_path):
    app, c = env
    r = await c.post("/api/v1/templates", json={"name": "X", "library_id": "nope", "rules": ALL})
    assert r.status_code == 400 and "unknown library 'nope'" in r.text
    r = await c.post(
        "/api/v1/templates", json={"name": "Y", "library_ids": ["music", "nope"], "rules": ALL}
    )
    assert r.status_code == 400 and "unknown library 'nope'" in r.text
    r = await c.post(
        "/api/v1/templates", json={"name": "Skryabin", "library_ids": ["music"], "rules": ALL}
    )
    assert r.status_code == 201 and r.json()["library_ids"] == ["music"]
    assert "library_id" not in r.json()
    assert (
        await c.patch("/api/v1/templates/skryabin", json={"library_id": "nope"})
    ).status_code == 400
    r = await c.patch("/api/v1/templates/skryabin", json={"library_ids": []})
    assert r.status_code == 200 and r.json()["library_ids"] == []
    r = await c.patch("/api/v1/templates/skryabin", json={"library_id": "music"})  # old clients
    assert r.status_code == 200 and r.json()["library_ids"] == ["music"]
    copy = (await c.post("/api/v1/templates/skryabin/copy", json={})).json()
    assert copy["library_ids"] == ["music"]
    # a copy of the library's default is a plain template of that library
    r = await c.post("/api/v1/templates/default.music.all/copy", json={})
    assert r.status_code == 200 and r.json()["library_ids"] == ["music"]
    r = await c.delete("/api/v1/libraries/music")
    assert r.status_code == 409 and "templates" in r.text


async def test_autocomplete_min_three_chars_anywhere_case_insensitive(env, music, tmp_path):
    app, c = env
    other = tmp_path / "other"
    from tests.media import make_music

    for artist in ("John Denver", "Pigeon John", "Hall & Oates"):
        make_music(other / f"{artist}.mp3", title="t", artist=artist, album="a")
    r = await c.post(
        "/api/v1/libraries",
        json={"id": "other", "type": "folder", "name": "Other", "settings": {"root": str(other)}},
    )
    assert r.status_code == 201

    async def values(q, libs=None):
        params = {"field": "artist", "q": q, **({"libraries": libs} if libs else {})}
        r = await c.get("/api/v1/rules/values", params=params)
        assert r.status_code == 200, r.text
        return sorted(v["value"] for v in r.json())

    assert await values("jo") == []  # 2 characters: nothing yet
    assert await values("john") == ["John Denver", "Pigeon John"]
    assert await values("JOHN", "other") == ["John Denver", "Pigeon John"]
    assert await values("john", "music") == []  # only from the given library
    assert await values("скр", "music") == ["Скрябін"]


async def test_rules_test_with_empty_library_ids_means_all(env):
    """[] = all libraries, like RuleTemplate.library_ids."""
    app, c = env
    body = {"rule_set": {"rules": ALL}}
    every = await c.post("/api/v1/rules/test", json=body | {"library_ids": None})
    assert every.status_code == 200, every.text
    r = await c.post("/api/v1/rules/test", json=body | {"library_ids": []})
    assert r.status_code == 200, r.text
    assert r.json()["items"] == every.json()["items"]

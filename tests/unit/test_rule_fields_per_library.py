"""the rule editor offers only the fields a device's libraries have (Navidrome: no audiobook
author; Audiobookshelf: no album), and a rule test warns about a field a library does not have."""

# ruff: noqa: F811 - pytest fixtures imported from test_api_sync
from __future__ import annotations

from syncmusic.core import rule_test
from syncmusic.core.models import LibraryConfig, Rule
from syncmusic.core.rules.fields import ALL_FIELDS, FIELDS
from syncmusic.core.rules.parser import fields_used
from syncmusic.libraries.audiobookshelf import AudiobookshelfLibrary
from syncmusic.libraries.folder import FolderLibrary
from syncmusic.libraries.subsonic import SubsonicLibrary
from tests.unit.test_api_sync import _removable_true, env, music  # noqa: F401


def test_field_lists_per_library_type():
    assert "author" not in SubsonicLibrary.rule_fields and "album" in SubsonicLibrary.rule_fields
    assert "series" in SubsonicLibrary.rule_fields  # podcasts
    assert "album" not in AudiobookshelfLibrary.rule_fields
    assert {
        "author",
        "series",
        "chapter",
        "position",
        "finished",
    } <= AudiobookshelfLibrary.rule_fields
    assert (
        FolderLibrary.rule_fields == ALL_FIELDS
    )  # tags of any file: music, podcasts and audiobooks
    for cls in (SubsonicLibrary, AudiobookshelfLibrary):
        assert cls.rule_fields <= set(FIELDS)  # no typo in the sets


def test_fields_used():
    crit = {
        "all": [
            {"is": {"author": "x"}},
            {"any": [{"gt": {"rating": 3}}, {"contains": {"myTag": "y"}}]},
        ]
    }
    assert fields_used(crit) == {"author", "rating"}  # a custom tag is not a known field
    assert fields_used({"bogus-op": 1}) == set()


async def _add(app, lid, type_, name):
    settings = {"url": "http://127.0.0.1:9", "username": "u", "password": "p"}
    if type_ == "audiobookshelf":
        settings = {"url": "http://127.0.0.1:9", "api_token": "t"}
    await app.store.put_library(LibraryConfig(id=lid, type=type_, name=name, settings=settings))


async def test_fields_api_marks_what_the_libraries_have(env):
    app, c = env
    await _add(app, "nd", "subsonic", "Navidrome")
    await _add(app, "abs", "audiobookshelf", "ABS")

    def avail(res):
        return {f["name"] for f in res["fields"] if f["available"]}

    every = avail((await c.get("/api/v1/rules/fields")).json())
    nd = avail((await c.get("/api/v1/rules/fields", params={"libraries": "nd"})).json())
    abs_ = avail((await c.get("/api/v1/rules/fields", params={"libraries": "abs"})).json())
    both = avail((await c.get("/api/v1/rules/fields", params={"libraries": "nd,abs"})).json())
    assert "author" in every and "album" in every
    assert "author" not in nd and "album" in nd
    assert "album" not in abs_ and "author" in abs_
    assert both == nd | abs_  # several libraries: the union
    # latestEpisodes is the builder's pseudo field `latest_episodes`, offered where podcasts are
    assert "latest_episodes" in every and "latest_episodes" in abs_ and "latest_episodes" in nd
    ops = (await c.get("/api/v1/rules/fields")).json()["operators"]
    assert ops["episodes"] == ["latestEpisodes"]


async def test_rule_warns_about_a_field_a_library_lacks(env):
    app, c = env
    await _add(app, "nd", "subsonic", "Navidrome")
    rules = [
        Rule(id="books", name="Books by", criteria={"is": {"author": "Франко"}}),
        Rule(id="any", criteria={"is": {"title": "x"}}),
    ]
    warnings = await rule_test.field_warnings(app.sync, rules, ["nd", "music"])
    assert warnings == [
        "rule 'Books by': author not available in library 'Navidrome', so the rule matches nothing there"
    ]


async def test_a_rule_limited_to_libraries_warns_only_for_those(env):
    """a device's picks rule draws from its own library only; a second library lacking the field is no
    reason to warn. Without `libraries` the rule still warns for every library."""
    app, c = env
    await _add(app, "nd", "subsonic", "Navidrome")
    await _add(app, "abs", "audiobookshelf", "ABS")
    crit = {"is": {"author": "x"}}
    limited = Rule(id="own", name="Picks", criteria=crit, libraries=["abs"])
    assert await rule_test.field_warnings(app.sync, [limited], ["nd", "abs"]) == []
    every = Rule(id="all", name="All", criteria=crit)
    assert await rule_test.field_warnings(app.sync, [every], ["nd", "abs"]) == [
        "rule 'All': author not available in library 'Navidrome', so the rule matches nothing there"
    ]

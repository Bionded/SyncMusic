"""every library gets its default rule templates (by type), generated until edited; edited ones are
stored, deleted ones stay deleted, removing the library removes its untouched ones."""

# ruff: noqa: F811 - pytest fixtures imported from test_api_sync
from __future__ import annotations

from syncmusic.core.models import (
    ItemRef,
    LibraryConfig,
    MediaKind,
    Playlist,
    RuleSet,
)
from syncmusic.core.rules import evaluate_ruleset
from syncmusic.core.rules.defaults import generated
from tests.factories import snapshot, track
from tests.unit.test_api_sync import env, music  # noqa: F401

ND = LibraryConfig(id="nd", type="subsonic", name="Navidrome", settings={"url": "http://x"})
ABS = LibraryConfig(id="abs", type="audiobookshelf", name="ABS", settings={"url": "http://x"})
JF = LibraryConfig(id="jf", type="jellyfin", name="Jellyfin", settings={"url": "http://x"})
FOLDER = LibraryConfig(
    id="f",
    type="folder",
    name="Files",
    settings={"root": "/x", "subfolder_kinds": {"Pods": "podcast"}},
)


def _keys(lib):
    return [t.id.removeprefix(f"default.{lib.id}.") for t in generated(lib)]


def test_defaults_per_library_type():
    assert _keys(ND) == ["all", "all-playlists", "favourites", "most-popular", "new"]
    assert _keys(ABS) == [
        "all-books",
        "all-podcasts",
        "unfinished-books",
        "books-in-progress",
        "unlistened-episodes",
        "latest-episodes",
        "unplayed-episodes",
    ]
    assert _keys(FOLDER) == ["all", "all-podcast"]
    assert _keys(JF) == ["all", "all-movies", "all-series"]
    for lib in (ND, ABS, FOLDER, JF):
        for t in generated(lib):
            assert t.library_ids == [lib.id] and t.default_of == lib.id and not t.edited
            assert t.rules[0].id == t.id  # the priority order can list the template like a rule


def test_folder_all_takes_only_its_kind_beside_other_kind_subfolders():
    """ "All music" of a folder with a podcast subfolder sends no podcast; "All podcasts" takes them."""
    ts = [track("s1", library_id="f"), track("p1", library_id="f", kind=MediaKind.PODCAST)]
    snaps = {"f": snapshot(ts, "f")}
    tpls = {t.id: t for t in generated(FOLDER)}

    def sel(key):
        rs = RuleSet(template_ids=[f"default.f.{key}"])
        return sorted(evaluate_ruleset(rs, snaps, tpls).items)

    assert tpls["default.f.all"].name == "All music"
    assert sel("all") == ["f/s1"] and sel("all-podcast") == ["f/p1"]
    plain = LibraryConfig(id="g", type="folder", name="G", settings={"root": "/y"})
    assert generated(plain)[0].rules[0].kind is None  # one kind only: every file, as before


def _nd_snapshot():
    ts = [
        track("1", library_id="nd", starred=True, play_count=3),
        track("2", library_id="nd", rating=4, play_count=50),
        track("3", library_id="nd", rating=3, play_count=0),
        track("4", library_id="nd", play_count=7),
        track("5", library_id="nd"),
    ]
    pls = [
        Playlist(ref=ItemRef(library_id="nd", item_id="p1"), name="Road", track_refs=[ts[2].ref]),
        Playlist(ref=ItemRef(library_id="nd", item_id="p2"), name="Gym", track_refs=[ts[3].ref]),
    ]
    return {"nd": snapshot(ts, "nd", playlists=pls)}


def _select(key):
    tpls = {t.id: t for t in generated(ND)}
    return evaluate_ruleset(RuleSet(template_ids=[f"default.nd.{key}"]), _nd_snapshot(), tpls)


def test_the_navidrome_defaults_do_what_the_former_builtins_did():
    sel = _select("all")
    assert sorted(sel.items) == ["nd/1", "nd/2", "nd/3", "nd/4", "nd/5"]
    assert sorted(p.name for p in sel.playlists) == ["Gym", "Road"]
    assert sorted(_select("favourites").items) == ["nd/1", "nd/2"]
    sel = _select("all-playlists")
    assert sorted(sel.items) == ["nd/3", "nd/4"]
    assert sorted(p.name for p in sel.playlists) == ["Gym", "Road"]
    assert list(_select("most-popular").items) == ["nd/2", "nd/4", "nd/1"]


def test_the_jellyfin_defaults_select_movies_and_series():
    videos = [
        track("m1", "jf", kind=MediaKind.VIDEO),
        track("e1", "jf", kind=MediaKind.VIDEO, series="Серіал"),
        track("e2", "jf", kind=MediaKind.VIDEO, series="Серіал"),
    ]
    snaps = {"jf": snapshot(videos, "jf")}
    tpls = {t.id: t for t in generated(JF)}

    def sel(key):
        rs = RuleSet(template_ids=[f"default.jf.{key}"])
        return sorted(evaluate_ruleset(rs, snaps, tpls).items)

    assert sel("all") == ["jf/e1", "jf/e2", "jf/m1"]
    assert sel("all-movies") == ["jf/m1"]
    assert sel("all-series") == ["jf/e1", "jf/e2"]


def test_the_abs_defaults_select_by_listening_state():
    books = [
        track("b1", "abs", kind=MediaKind.AUDIOBOOK, position_s=10.0, finished=False),
        track("b2", "abs", kind=MediaKind.AUDIOBOOK, finished=True, position_s=99.0),
        track("b3", "abs", kind=MediaKind.AUDIOBOOK),
        track("e1", "abs", kind=MediaKind.PODCAST, series="P"),
        track("e2", "abs", kind=MediaKind.PODCAST, series="P", position_s=5.0),
    ]
    snaps = {"abs": snapshot(books, "abs")}
    tpls = {t.id: t for t in generated(ABS)}

    def sel(key):
        rs = RuleSet(template_ids=[f"default.abs.{key}"])
        return sorted(evaluate_ruleset(rs, snaps, tpls).items)

    assert sel("all-books") == ["abs/b1", "abs/b2", "abs/b3"]
    assert sel("all-podcasts") == ["abs/e1", "abs/e2"]
    assert sel("unfinished-books") == ["abs/b1", "abs/b3"]
    assert sel("books-in-progress") == ["abs/b1"]
    assert sel("unlistened-episodes") == ["abs/e1"]


async def test_added_edited_deleted_and_removed_with_the_library(env, music):
    app, c = env
    listed = {t["id"]: t for t in (await c.get("/api/v1/templates")).json()}
    assert set(listed) == {"default.music.all"}  # the folder library of the fixture
    assert listed["default.music.all"]["name"] == "All music"
    # adding a library adds its defaults
    r = await c.post(
        "/api/v1/libraries",
        json={
            "id": "more",
            "type": "folder",
            "name": "More",
            "settings": {"root": str(music / "Скрябін")},
        },
    )
    assert r.status_code == 201, r.text
    assert "default.more.all" in {t["id"] for t in (await c.get("/api/v1/templates")).json()}
    # untouched: generated, not stored (not in a settings backup)
    assert await app.db.fetchone("SELECT id FROM templates") is None
    # edited: stored, still the library's default
    r = await c.patch("/api/v1/templates/default.music.all", json={"name": "Everything"})
    assert r.status_code == 200, r.text
    assert r.json()["edited"] is True and r.json()["default_of"] == "music"
    row = await app.db.fetchone("SELECT id FROM templates")
    assert row["id"] == "default.music.all"
    # deleted: not generated again
    assert (await c.delete("/api/v1/templates/default.more.all")).status_code == 204
    ids = {t["id"] for t in (await c.get("/api/v1/templates")).json()}
    assert ids == {"default.music.all"}
    assert (await c.get("/api/v1/templates/default.more.all")).status_code == 404
    # the reserved prefix
    r = await c.post("/api/v1/templates", json={"id": "default.music.x", "name": "x"})
    assert r.status_code == 400
    # a copy is a plain user template
    draft = (await c.post("/api/v1/templates/default.music.all/copy", json={})).json()
    assert "default_of" not in draft and "edited" not in draft
    # removing a library: its untouched defaults and deleted marks go
    assert (await c.delete("/api/v1/libraries/more")).status_code == 204
    assert await app.db.fetchone("SELECT id FROM deleted_default_templates") is None
    # an edited one: the caller chooses
    r = await c.delete("/api/v1/libraries/music")
    assert r.status_code == 409 and "edited default templates (Everything)" in r.text
    r = await c.delete("/api/v1/libraries/music", params={"edited_defaults": "keep"})
    assert r.status_code == 204, r.text
    # kept under a fresh id from its name, the reserved one is free again
    assert (await c.get("/api/v1/templates/default.music.all")).status_code == 404
    kept = (await c.get("/api/v1/templates/everything")).json()
    assert kept["name"] == "Everything" and kept["default_of"] is None and kept["library_ids"] == []


async def test_removing_a_library_can_delete_its_edited_defaults(env):
    app, c = env
    r = await c.patch("/api/v1/templates/default.music.all", json={"name": "Everything"})
    assert r.status_code == 200
    r = await c.delete("/api/v1/libraries/music", params={"edited_defaults": "delete"})
    assert r.status_code == 204, r.text
    assert (await c.get("/api/v1/templates")).json() == []
    assert await app.db.fetchone("SELECT id FROM deleted_default_templates") is None
    assert (
        await c.delete("/api/v1/libraries/x", params={"edited_defaults": "nope"})
    ).status_code in (400, 422)


async def test_a_kept_default_does_not_hide_the_defaults_of_a_library_added_again(
    env, music, tmp_path
):
    """ "Remove, keep the templates" kept the edited default under its reserved id default.<lib>.<key>; a
    library added again with the same id then got no "All music" of its own. Now it gets a fresh id; a device that
    linked it links the new id with its priorities unchanged."""
    app, c = env
    r = await c.patch("/api/v1/templates/default.music.all", json={"name": "Everything"})
    assert r.status_code == 200
    rule_ids = [r["id"] for r in r.json()["rules"]]
    (tmp_path / "stick").mkdir()
    rs = {"template_ids": ["default.music.all"], "priority": rule_ids}
    body = {
        "name": "Stick",
        "mount_path": str(tmp_path / "stick"),
        "type": "flashdrive",
        "rule_set": rs,
    }
    r = await c.post("/api/v1/devices/register", json=body)
    assert r.status_code == 201, r.text
    dev = r.json()["id"]
    r = await c.delete("/api/v1/libraries/music", params={"edited_defaults": "keep"})
    assert r.status_code == 204, r.text
    linked = (await c.get(f"/api/v1/devices/{dev}")).json()["config"]["rule_set"]
    assert linked["template_ids"] == ["everything"] and linked["priority"] == rule_ids
    r = await c.post(
        "/api/v1/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
    )
    assert r.status_code == 201, r.text
    listed = (await c.get("/api/v1/templates")).json()
    assert sorted(t["name"] for t in listed) == ["All music", "Everything"]
    assert [t["name"] for t in listed if t.get("default_of") == "music"] == ["All music"]


async def test_saving_an_untouched_default_unchanged_keeps_it_generated(env):
    """the template editor's Save sends name, libraries, rules and excludes (PATCH) even when nothing was
    changed; a default saved like that must stay a generated, untouched default (not stored, not in a backup)."""
    app, c = env
    tpl = (await c.get("/api/v1/templates/default.music.all")).json()
    body = {k: tpl[k] for k in ("name", "library_ids", "rules", "excludes")}
    r = await c.patch("/api/v1/templates/default.music.all", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["edited"] is False
    assert await app.db.fetchone("SELECT id FROM templates") is None


async def test_builder_wrappers_do_not_count_but_a_real_change_does(env, music):
    """a single condition the builder wraps in `all` is the same rule; a real change still marks the
    default edited."""
    from syncmusic.facade import _plain

    app, c = env
    tpl = (await c.get("/api/v1/templates/default.music.all")).json()
    body = {k: tpl[k] for k in ("name", "library_ids", "rules", "excludes")}
    r = await c.patch("/api/v1/templates/default.music.all", json=body)
    assert r.status_code == 200 and r.json()["edited"] is False
    assert (await c.get("/api/v1/templates/default.music.all")).json()["edited"] is False
    latest = {"latestEpisodes": {"per_series": 3}}
    assert _plain({"all": [latest]}) == latest and _plain({"all": [latest, latest]}) != latest
    r = await c.patch("/api/v1/templates/default.music.all", json={**body, "name": "Усе"})
    assert r.json()["edited"] is True


async def test_reset_an_edited_default(env, music):
    """Reset to default drops the stored copy; the generated default is back (not edited, not stored)."""
    app, c = env
    tpl = (await c.get("/api/v1/templates/default.music.all")).json()
    body = {k: tpl[k] for k in ("name", "library_ids", "rules", "excludes")}
    r = await c.patch("/api/v1/templates/default.music.all", json={**body, "name": "Усе"})
    assert r.json()["edited"] is True
    r = await c.post("/api/v1/templates/default.music.all/reset")
    assert r.status_code == 200, r.text
    assert r.json()["edited"] is False and r.json()["name"] == tpl["name"]
    assert await app.db.fetchone("SELECT id FROM templates") is None
    # an untouched default or a user's template cannot be reset
    assert (await c.post("/api/v1/templates/default.music.all/reset")).status_code == 400
    mine = (await c.post("/api/v1/templates", json={**body, "name": "Mine", "id": "mine"})).json()
    assert (await c.post(f"/api/v1/templates/{mine['id']}/reset")).status_code == 400
    assert (await c.post("/api/v1/templates/nope/reset")).status_code == 404


def test_video_folder_library_has_defaults():
    """A folder library of videos must not break the generated defaults: every page builds them."""
    for settings in (
        {"root": "/v", "default_kind": "video"},
        {"root": "/m", "subfolder_kinds": {"Video": "video"}},
    ):
        lib = LibraryConfig(id="vid", type="folder", name="Відео", settings=settings)
        assert generated(lib)


async def test_video_folder_library_keeps_the_api_working(env, tmp_path):
    """a video folder library made /devices, /templates, refresh and its own delete answer 500."""
    app, c = env
    vid = tmp_path / "vid" / "Мультики" / "Season 01"
    vid.mkdir(parents=True)
    (vid / "Episode 01.mp4").write_bytes(b"\0" * 64)
    lib = {
        "id": "video",
        "type": "folder",
        "name": "Відео",
        "settings": {"root": str(tmp_path / "vid"), "default_kind": "video"},
    }
    assert (await c.post("/api/v1/libraries", json=lib)).status_code == 201
    assert (await c.get("/api/v1/devices")).status_code == 200
    r = await c.post("/api/v1/libraries/video/refresh")
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded", job.error
    r = await c.get("/api/v1/templates")
    assert r.status_code == 200
    assert {"default.video.all": "All videos"}.items() <= {
        t["id"]: t["name"] for t in r.json()
    }.items()
    assert (await c.delete("/api/v1/libraries/video")).status_code in (200, 204)
    ids = {t["id"] for t in (await c.get("/api/v1/templates")).json()}
    assert not any(i.startswith("default.video.") for i in ids)

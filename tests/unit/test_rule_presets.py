"""Rule presets: each builds a valid rule that selects what its name says."""

# ruff: noqa: F811 - pytest fixtures imported from test_api_sync

from datetime import timedelta

import pytest

from syncmusic.core.models import MediaKind
from syncmusic.core.rules import validate_rule
from syncmusic.core.rules.evaluator import EvalContext, evaluate_rule
from syncmusic.core.rules.fields import BOOK_FIELDS, MUSIC_FIELDS
from syncmusic.core.rules.presets import PRESETS, available
from tests.factories import NOW, snapshot, track
from tests.unit.test_api_sync import _removable_true, env, music  # noqa: F401
from tests.unit.test_rule_fields_per_library import _add

DAY = timedelta(days=1)


@pytest.fixture
def snaps():
    music = [
        track("fav", starred=True, play_count=3, date_added=NOW - 400 * DAY),
        track("five", rating=5, play_count=9, date_added=NOW - 200 * DAY),
        track("three", rating=3, date_added=NOW - 5 * DAY),
        track("new", date_added=NOW - 29 * DAY, play_count=1),
        track("old", date_added=NOW - 31 * DAY),
    ]
    books = [
        track("started", kind=MediaKind.AUDIOBOOK, position_s=120.0, finished=False),
        track("done", kind=MediaKind.AUDIOBOOK, position_s=3000.0, finished=True),
        track("unopened", kind=MediaKind.AUDIOBOOK),
    ]
    pods = [
        track(f"ep{i}", kind=MediaKind.PODCAST, series="Pod", published=NOW - i * DAY)
        for i in range(5)
    ]
    return {"lib": snapshot(music + books + pods)}


def run(snaps, preset_id, **params):
    rule = PRESETS[preset_id].rule(params)
    res = validate_rule(rule)
    assert res.errors == [] and res.warnings == [], res
    return [t.ref.item_id for t in evaluate_rule(rule, EvalContext(snaps, NOW)).tracks]


def test_each_preset_selects_what_it_says(snaps):
    assert sorted(run(snaps, "all-music")) == ["fav", "five", "new", "old", "three"]
    assert PRESETS["all-music"].rule().include_playlists
    assert sorted(run(snaps, "favourites")) == ["fav", "five"]
    assert sorted(run(snaps, "favourites", stars=3)) == ["fav", "five", "three"]
    assert sorted(run(snaps, "new")) == ["new", "three"]
    assert sorted(run(snaps, "new", days=7)) == ["three"]
    assert run(snaps, "most-played") == ["five", "fav", "new"]
    assert run(snaps, "most-played", tracks=2) == ["five", "fav"]
    assert run(snaps, "picked") == []  # filled by "Pick from library" in the builder
    assert run(snaps, "books-in-progress") == ["started"]
    assert sorted(run(snaps, "latest-episodes")) == ["ep0", "ep1", "ep2"]
    assert sorted(run(snaps, "latest-episodes", episodes=1)) == ["ep0"]


def test_names_follow_the_parameters_and_ids_are_new():
    a, b = PRESETS["new"].rule({"days": 7}), PRESETS["new"].rule({"days": 7})
    assert a.name == "Added in the last 7 days" and a.id != b.id
    assert a.kind == [MediaKind.MUSIC]


@pytest.mark.parametrize(
    "params",
    [{"days": 0}, {"days": 1.5}, {"days": "30"}, {"days": True}, {"weeks": 2}],
)
def test_bad_parameters_are_refused(params):
    with pytest.raises(ValueError):
        PRESETS["new"].rule(params)


def test_availability_by_library_kind_and_fields():
    navidrome = ({MediaKind.MUSIC, MediaKind.PODCAST}, MUSIC_FIELDS | {"series", "published"})
    abs_ = ({MediaKind.AUDIOBOOK, MediaKind.PODCAST}, BOOK_FIELDS)
    on = lambda libs: {p.id for p in PRESETS.values() if available(p, libs)}  # noqa: E731
    # Navidrome's podcasts have no "finished": no "newest unplayed" for them
    assert on([navidrome]) == set(PRESETS) - {"books-in-progress", "unplayed-episodes"}
    assert on([abs_]) == {"books-in-progress", "latest-episodes", "unplayed-episodes"}
    assert on([navidrome, abs_]) == set(PRESETS)
    assert on([]) == set()


async def test_presets_api(env):
    app, c = env
    await _add(app, "abs", "audiobookshelf", "ABS")
    offered = {p["id"]: p for p in (await c.get("/api/v1/rule-presets")).json()}
    assert set(offered) == set(PRESETS)
    assert offered["new"]["params"] == [
        {"name": "days", "label": "Days", "default": 30, "min": 1, "max": 3650}
    ]
    res = (await c.get("/api/v1/rule-presets", params={"libraries": "abs"})).json()
    assert {p["id"] for p in res if p["available"]} == {
        "books-in-progress",
        "latest-episodes",
        "unplayed-episodes",
    }
    rule = (await c.post("/api/v1/rule-presets/new/rule", json={"params": {"days": 7}})).json()
    assert rule["criteria"] == {"inTheLast": {"dateadded": 7}} and rule["kind"] == ["music"]
    assert (await c.post("/api/v1/rule-presets/new/rule")).json()["name"] == (
        "Added in the last 30 days"
    )
    bad = await c.post("/api/v1/rule-presets/new/rule", json={"params": {"days": 0}})
    assert bad.status_code == 400 and "Days" in bad.text
    assert (await c.post("/api/v1/rule-presets/nope/rule")).status_code == 404


def test_a_generated_name_follows_its_number_a_typed_one_never():
    """the builder changes the number of a preset rule (it wraps the condition in `all`) → the generated
    name says the new number; a name the user typed stays."""
    from syncmusic.core.rules.presets import follow_name

    ep = PRESETS["latest-episodes"].rule({"episodes": 3})
    five = ep.model_copy(update={"criteria": {"all": [{"latestEpisodes": {"per_series": 5}}]}})
    assert follow_name(five).name == "The latest 5 episodes of every podcast"
    assert follow_name(ep) == ep  # unchanged
    fav = PRESETS["favourites"].rule({"stars": 4})
    three = fav.model_copy(
        update={"criteria": {"any": [{"is": {"loved": True}}, {"gt": {"rating": 2}}]}}
    )
    assert follow_name(three).name == "Favourites: starred or rated 3 stars or more"
    new = PRESETS["new"].rule({"days": 30})
    week = new.model_copy(update={"criteria": {"inTheLast": {"dateadded": 7}}})
    assert follow_name(week).name == "Added in the last 7 days"
    top = PRESETS["most-played"].rule().model_copy(update={"limit": 50})
    assert follow_name(top).name == "The 50 most played tracks"
    typed = top.model_copy(update={"name": "My top tracks"})
    assert follow_name(typed).name == "My top tracks"


async def test_saved_template_renames_a_generated_rule_name(env):
    app, c = env
    rule = PRESETS["new"].rule({"days": 30}).model_dump(mode="json")
    r = await c.post("/api/v1/templates", json={"name": "Neu", "rules": [rule]})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    rule["criteria"] = {"all": [{"inTheLast": {"dateadded": 14}}]}
    typed = {**rule, "id": "typed", "name": "Fresh stuff"}
    r = await c.patch(f"/api/v1/templates/{tid}", json={"rules": [rule, typed]})
    assert r.status_code == 200, r.text
    assert [x["name"] for x in r.json()["rules"]] == ["Added in the last 14 days", "Fresh stuff"]


def test_newest_unplayed_episodes_make_room(snaps):
    """with `unplayed` a finished episode leaves and the next newest comes; without, it leaves a gap."""
    pods = [
        track(
            f"ep{i}", kind=MediaKind.PODCAST, series="Pod", published=NOW - i * DAY, finished=i == 0
        )
        for i in range(5)
    ]
    snaps = {"lib": snapshot(pods)}
    assert sorted(run(snaps, "unplayed-episodes", episodes=2)) == ["ep1", "ep2"]
    assert sorted(run(snaps, "latest-episodes", episodes=2)) == ["ep0", "ep1"]
    rule = PRESETS["unplayed-episodes"].rule({"episodes": 2})
    assert rule.criteria == {"latestEpisodes": {"per_series": 2, "unplayed": True}}
    from syncmusic.core.rules import RuleError, parse_criteria

    with pytest.raises(RuleError):
        parse_criteria({"latestEpisodes": {"per_series": 2, "unplayed": "yes"}})
    with pytest.raises(RuleError):
        parse_criteria({"latestEpisodes": {"per_series": 2, "other": 1}})

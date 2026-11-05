import json
import time
from pathlib import Path

import pytest

from syncmusic.core.models import MediaKind, Rule, RuleSet, RuleTemplate
from syncmusic.core.rules import (
    RuleError,
    evaluate_ruleset,
    parse_criteria,
    rule_from_nsp,
    rule_from_text,
    rule_to_json,
    rule_to_nsp,
    rule_to_yaml,
    ruleset_from_text,
    validate_rule,
    validate_ruleset,
)
from syncmusic.core.rules.evaluator import EvalContext, evaluate_rule
from syncmusic.core.rules.parser import parse_duration, parse_size
from syncmusic.core.rules.templates import (
    MANUAL_RULE_ID,
    add_to_manual_selection,
    remove_from_manual_selection,
    template_usage,
)
from tests.factories import NOW, music_library, snapshot, track

NSP_DIR = Path(__file__).parent / "data" / "nsp"


@pytest.fixture
def snaps():
    return {"lib": music_library()}


def ids(snaps, criteria, **rule_kw):
    rule = Rule(id="r", criteria=criteria, **rule_kw)
    res = evaluate_rule(rule, EvalContext(snaps, NOW))
    return [t.ref.item_id for t in res.tracks]


def sids(snaps, criteria, **kw):
    return sorted(ids(snaps, criteria, **kw))


# --- operators ----------------------------------------------------------------------------------


def test_is_isnot_strings_case_insensitive(snaps):
    assert sids(snaps, {"is": {"artist": "ac/dc"}}) == ["1", "2"]
    assert "1" not in ids(snaps, {"isNot": {"artist": "AC/DC"}})
    assert sids(snaps, {"is": {"genre": "hard rock"}}) == ["1"]  # multi-valued


def test_is_bool_and_number(snaps):
    assert sids(snaps, {"is": {"loved": True}}) == ["1"]
    assert sids(snaps, {"is": {"compilation": True}}) == ["4"]
    assert sids(snaps, {"is": {"rating": 3}}) == ["2"]
    assert "3" in ids(snaps, {"is": {"rating": 0}})  # unrated = 0 (Navidrome)


def test_gt_lt(snaps):
    assert sids(snaps, {"gt": {"rating": 3}}) == ["1", "4"]
    assert sids(snaps, {"lt": {"year": 1981, "playcount": 100}}) == ["1", "2", "5", "6", "7", "8"]
    assert sids(snaps, {"gt": {"lastplayed": "2026-09-25"}}) == ["1"]


def test_contains_family(snaps):
    assert sids(snaps, {"contains": {"title": "bell"}}) == ["2"]
    assert "2" not in ids(snaps, {"notContains": {"title": "BELL"}})
    assert sids(snaps, {"startsWith": {"album": "hunting"}}) == ["3"]
    assert sids(snaps, {"endsWith": {"title": "крові"}}) == ["4"]
    assert sids(snaps, {"contains": {"comment": "master"}}) == ["3"]


def test_in_the_range(snaps):
    assert sids(snaps, {"inTheRange": {"year": [1981, 1990]}}) == ["3", "4"]
    assert sids(snaps, {"inTheRange": {"year": [1990, 1981]}}) == ["3", "4"]  # swapped bounds
    assert sids(snaps, {"inTheRange": {"dateadded": ["2026-09-01", "2026-10-01"]}}) == [
        "3",
        "5",
        "6",
        "7",
    ]


def test_before_after(snaps):
    assert sids(snaps, {"before": {"dateadded": "2025-10-01"}}) == ["1", "2"]
    assert sids(snaps, {"after": {"published": "2026-09-05"}}) == ["6", "7"]


def test_in_the_last(snaps):
    assert sids(snaps, {"inTheLast": {"lastplayed": 30}}) == ["1", "4"]
    # never played counts as "not in the last"
    assert sids(snaps, {"notInTheLast": {"lastplayed": 30}}) == ["2", "3", "5", "6", "7", "8"]


def test_in_playlist(snaps):
    assert sids(snaps, {"inPlaylist": {"id": "pl1"}}) == ["1", "3"]
    assert sids(snaps, {"inPlaylist": {"name": "favourites"}}) == ["1", "3"]
    assert "1" not in ids(snaps, {"notInPlaylist": {"id": "pl1"}})
    assert sids(snaps, {"inPlaylist": {"path": "../Favourites.nsp"}}) == ["1", "3"]


def test_unknown_playlist_warns(snaps):
    def warnings(criteria):
        return evaluate_rule(Rule(id="r", criteria=criteria), EvalContext(snaps, NOW)).warnings

    assert warnings({"inPlaylist": {"id": "pl1"}}) == []
    assert warnings({"notInPlaylist": {"any": True}}) == []
    gone = warnings({"any": [{"inPlaylist": {"id": "gone"}}, {"notInPlaylist": {"name": "Nope"}}]})
    assert gone == [
        "no playlist matches inPlaylist {'id': 'gone'}",
        "no playlist matches notInPlaylist {'name': 'Nope'}",
    ]


def test_missing_present(snaps):
    assert sids(snaps, {"isMissing": {"genre": True}}) == ["5", "6", "7", "8"]
    assert sids(snaps, {"isPresent": {"genre": True}}) == ["1", "2", "3", "4"]
    assert sids(snaps, {"isMissing": {"genre": False}}) == ["1", "2", "3", "4"]


def test_extension_in_notin(snaps):
    assert sids(snaps, {"in": {"album_id": ["al1", "al3"]}}) == ["1", "2", "4"]
    assert sids(snaps, {"in": {"track_id": ["3"]}}) == ["3"]
    assert "1" not in ids(snaps, {"notIn": {"artist_id": ["ar1"]}})


def test_kind_series_finished(snaps):
    assert sids(snaps, {"is": {"kind": "podcast"}}) == ["5", "6", "7"]
    assert sids(snaps, {"all": [{"is": {"series": "pod"}}, {"gt": {"position": 0}}]}) == ["7"]
    assert sids(snaps, {"is": {"finished": True}}) == ["8"]


def test_latest_episodes(snaps):
    assert sids(snaps, {"latestEpisodes": {"per_series": 2}}) == ["6", "7"]


def test_nested_groups(snaps):
    c = {
        "all": [
            {"any": [{"is": {"loved": True}}, {"gt": {"rating": 3}}]},
            {"inTheRange": {"year": [1981, 1990]}},
        ]
    }
    assert ids(snaps, c) == ["4"]


def test_unknown_field_warns_and_uses_extra(snaps):
    res = parse_criteria({"is": {"mood": "dark"}})
    assert res.warnings and "unknown field" in res.warnings[0]
    assert ids(snaps, {"is": {"mood": "dark"}}) == ["4"]
    # unknown field, negated: tracks without the tag never match
    assert ids(snaps, {"isNot": {"nosuchfield": "x"}}) == []


def test_extra_prefix(snaps):
    assert ids(snaps, {"is": {"extra.mood": "dark"}}) == ["4"]


def test_album_aggregates(snaps):
    assert sids(snaps, {"gt": {"albumplaycount": 13}}) == ["1", "2"]
    assert sids(snaps, {"is": {"albumsongcount": 2}}) == ["1", "2"]


@pytest.mark.parametrize(
    "bad",
    [
        {"foo": {"title": "x"}},
        {"all": {"is": {"title": "x"}}},
        {"inTheRange": {"year": [1]}},
        {"inTheLast": {"lastplayed": "x"}},
        {"before": {"dateadded": "yesterday"}},
        {"in": {"album_id": "al1"}},
        {"is": {}},
        {"inPlaylist": {"foo": 1}},
        {"latestEpisodes": {"per_series": 0}},
        {"inTheLast": {"title": 3}},
        [],
    ],
)
def test_invalid_criteria(bad):
    with pytest.raises(RuleError):
        parse_criteria(bad)


# --- sort / limit ---------------------------------------------------------------------------------


def test_sort_multi_and_order(snaps):
    music = {"is": {"kind": "music"}}
    assert ids(snaps, music, sort="-year,title") == ["4", "3", "1", "2"]
    assert ids(snaps, music, sort="year,title", order="desc") == ["4", "3", "2", "1"]
    assert ids(snaps, music, sort="playCount", order="desc", limit=2) == ["1", "4"]


def test_random_sort_is_stable(snaps):
    a = ids(snaps, {}, sort="random")
    assert a == ids(snaps, {}, sort="random") and sorted(a) == [str(i) for i in range(1, 9)]


def test_random_period_reshuffles_per_week_or_month(snaps):
    """a new seed each ISO week / month; none = the same order forever."""
    from datetime import timedelta

    from pydantic import ValidationError

    def order(now, period):
        rule = Rule(id="r", sort="random", random_period=period)
        return [t.ref.item_id for t in evaluate_rule(rule, EvalContext(snaps, now)).tracks]

    later = NOW + timedelta(days=14)
    assert order(NOW, "none") == order(later, "none") == ids(snaps, {}, sort="random")
    assert order(NOW, "week") != order(later, "week")
    monday = NOW - timedelta(days=NOW.weekday())
    assert order(monday, "week") == order(monday + timedelta(days=6), "week")
    assert order(NOW, "month") != order(NOW + timedelta(days=62), "month")
    with pytest.raises(ValidationError):
        Rule(id="r", sort="random", random_period="day")
    rule = Rule(id="r", sort="random", random_period="week")
    assert rule_from_text(rule_to_yaml(rule)).random_period == "week"
    doc, warnings = rule_to_nsp(rule)
    assert "random_period" not in doc and "extension setting 'random_period' dropped" in warnings


def test_limit_percent_and_size_duration(snaps):
    assert len(ids(snaps, {}, limit_percent=10)) == 1
    assert len(ids(snaps, {}, limit_percent=50)) == 4
    assert ids(snaps, {"is": {"kind": "music"}}, sort="title", limit_size="12MB") == ["1", "2"]
    assert len(ids(snaps, {}, limit_duration="10m")) == 3


def test_parse_size_duration():
    assert parse_size("10GB") == 10 * 1000**3 and parse_size("2GiB") == 2 * 1024**3
    assert parse_duration("1h30m") == 5400 and parse_duration("2d") == 172800
    with pytest.raises(RuleError):
        parse_size("lots")
    with pytest.raises(RuleError):
        parse_duration("1x")


# --- rule sets --------------------------------------------------------------------------------------


def test_ruleset_union_excludes_priority_templates(snaps):
    tpl = RuleTemplate(
        id="t1",
        name="Loved",
        rules=[Rule(id="loved", criteria={"is": {"loved": True}})],
        excludes=[Rule(id="nopod", criteria={"is": {"kind": "podcast"}})],
    )
    rs = RuleSet(
        template_ids=["t1", "missing"],
        rules=[
            Rule(id="rock", criteria={"is": {"genre": "rock"}}),
            Rule(id="pods", criteria={"is": {"series": "Pod"}}),
            Rule(id="off", enabled=False, criteria={}),
        ],
        excludes=[Rule(id="noacdc2", criteria={"in": {"track_id": ["2"]}})],
        priority=["rock"],
    )
    sel = evaluate_ruleset(rs, snaps, {"t1": tpl}, now=NOW)
    assert list(sel.items) == ["lib/1", "lib/4"]
    assert sel.items["lib/1"] == ["rock", "loved"]
    assert any("missing" in w for w in sel.warnings)


def test_ruleset_library_and_kind_restrictions():
    a = snapshot(
        [track("1", library_id="a"), track("2", library_id="a", kind=MediaKind.PODCAST)], "a"
    )
    b = snapshot([track("1", library_id="b")], "b")
    rs = RuleSet(rules=[Rule(id="r", libraries=["b"], criteria={})])
    assert list(evaluate_ruleset(rs, {"a": a, "b": b}).items) == ["b/1"]
    rs = RuleSet(rules=[Rule(id="r", kind=[MediaKind.PODCAST], criteria={})])
    assert list(evaluate_ruleset(rs, {"a": a, "b": b}).items) == ["a/2"]
    rs = RuleSet(rules=[Rule(id="r", criteria={})])
    sel = evaluate_ruleset(rs, {"a": a, "b": b}, allowed_kinds={MediaKind.MUSIC})
    assert sorted(sel.items) == ["a/1", "b/1"] and sel.warnings
    sel = evaluate_ruleset(rs, {"a": a, "b": b}, attached=["a"])
    assert sorted(sel.items) == ["a/1", "a/2"]


def test_playlists_from_rules(snaps):
    rs = RuleSet(
        rules=[
            Rule(id="r1", criteria={"is": {"genre": "rock"}}, sort="title", as_playlist="Rock"),
            Rule(id="r2", criteria={"inPlaylist": {"name": "Favourites"}}, include_playlists=True),
        ],
        excludes=[Rule(id="x", criteria={"in": {"track_id": ["2"]}})],
    )
    sel = evaluate_ruleset(rs, snaps, now=NOW)
    pls = {p.name: [r.item_id for r in p.refs] for p in sel.playlists}
    assert pls == {"Rock": ["1", "4"], "Favourites": ["1", "3"]}


def test_in_playlist_rule_puts_its_playlists_on_device_by_default(snaps):
    # no include_playlists needed; skip_playlists opts out
    rule = Rule(id="r", criteria={"inPlaylist": {"name": "Favourites"}})
    sel = evaluate_ruleset(RuleSet(rules=[rule]), snaps, now=NOW)
    assert [p.name for p in sel.playlists] == ["Favourites"]
    skip = rule.model_copy(update={"skip_playlists": True})
    sel = evaluate_ruleset(RuleSet(rules=[skip]), snaps, now=NOW)
    assert sel.playlists == [] and sorted(sel.items)
    plain = Rule(id="r", criteria={"is": {"genre": "rock"}})
    assert evaluate_ruleset(RuleSet(rules=[plain]), snaps, now=NOW).playlists == []


# --- text formats ------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(NSP_DIR.glob("*.nsp")), ids=lambda p: p.name)
def test_navidrome_examples_import_evaluate_roundtrip(path, snaps):
    rule = rule_from_nsp(path.read_text(), rule_id="x")
    assert validate_rule(rule).ok
    evaluate_rule(rule, EvalContext(snaps, NOW))  # never crashes
    doc, warnings = rule_to_nsp(rule)
    assert not warnings
    original = json.loads(path.read_text())
    assert doc == original | ({"order": "desc"} if original.get("order") == "desc" else {})


def test_navidrome_examples_semantics(snaps):
    def run(name):
        rule = rule_from_nsp((NSP_DIR / name).read_text(), rule_id="x")
        return [t.ref.item_id for t in evaluate_rule(rule, EvalContext(snaps, NOW)).tracks]

    assert run("example_01.nsp") == ["1", "4"]  # recently played, lastPlayed desc
    assert run("example_02.nsp") == ["4"]  # 80s favourites
    assert run("example_03.nsp") == ["1"]  # loved


def test_nsp_with_comments_and_order():
    rule = rule_from_nsp('{"all": [{"gt": {"playCount": -1}}],\n "sort": "random"\n // limit: 1\n}')
    assert rule.sort == "random" and rule.limit is None


def test_nsp_export_strips_extensions():
    rule = Rule(
        id="x",
        criteria={"all": [{"in": {"album_id": ["a"]}}, {"is": {"loved": True}}]},
        limit_size="1GB",
    )
    doc, warnings = rule_to_nsp(rule)
    assert doc == {"all": [{"is": {"loved": True}}]}
    assert any("in" in w for w in warnings) and any("limit_size" in w for w in warnings)


def test_json_yaml_roundtrip():
    rule = Rule(
        id="r1",
        name="Скрябін & co",
        criteria={"all": [{"is": {"artist": "Скрябін"}}, {"inTheLast": {"dateadded": 30}}]},
        sort="-year",
        limit=10,
        kind=[MediaKind.MUSIC],
        limit_size="2GB",
        as_playlist="Мix",
    )
    assert rule_from_text(rule_to_json(rule)) == rule
    assert rule_from_text(rule_to_yaml(rule)) == rule


def test_ruleset_from_yaml():
    rs = ruleset_from_text(
        """
rules:
  - id: loved
    criteria: {is: {loved: true}}
excludes:
  - id: x
    criteria: {contains: {genre: christmas}}
mode: additive
"""
    )
    assert rs.mode == "additive" and rs.rules[0].id == "loved"
    with pytest.raises(RuleError):
        ruleset_from_text("rules:\n  - id: a\n    criteria: {bogus: {x: 1}}\n")
    assert ruleset_from_text('{"all": [{"is": {"loved": true}}]}').rules[0].criteria


def test_validate_rule_reports_errors():
    res = validate_rule(Rule(id="r", criteria={"inTheRange": {"year": 3}}, limit_size="x"))
    assert len(res.errors) == 2


# --- templates & manual selection --------------------------------------------------------------------


def test_usage():
    from syncmusic.core.models import DeviceConfig

    rs = RuleSet(template_ids=["t"])
    devs = [
        DeviceConfig(id="d1", type="x", name="d", rule_set=rs),
        DeviceConfig(id="d2", type="x", name="e"),
    ]
    assert [d.id for d in template_usage("t", devs)] == ["d1"]


def test_template_edit_changes_every_linked_ruleset(snaps):
    tpl = RuleTemplate(id="t", name="T", rules=[Rule(id="loved", criteria={"is": {"loved": True}})])
    a = RuleSet(template_ids=["t"])
    b = RuleSet(template_ids=["t"], rules=[Rule(id="pods", criteria={"is": {"series": "Pod"}})])
    assert list(evaluate_ruleset(a, snaps, {"t": tpl}).items) == ["lib/1"]
    edited = tpl.model_copy(update={"rules": [Rule(id="rock", criteria={"is": {"genre": "rock"}})]})
    assert sorted(evaluate_ruleset(a, snaps, {"t": edited}).items) == ["lib/1", "lib/2", "lib/4"]
    assert sorted(evaluate_ruleset(b, snaps, {"t": edited}).items) == [
        "lib/1",
        "lib/2",
        "lib/4",
        "lib/5",
        "lib/6",
        "lib/7",
    ]


def test_template_dedup(snaps):
    tpl = RuleTemplate(id="t", name="T", rules=[Rule(id="loved", criteria={"is": {"loved": True}})])
    rs = RuleSet(
        template_ids=["t", "t"],  # linked twice
        rules=[Rule(id="rock", criteria={"is": {"genre": "hard rock"}})],
    )
    sel = evaluate_ruleset(rs, snaps, {"t": tpl})
    assert sel.items == {"lib/1": ["loved", "rock"]}  # one item, both reasons, no repeats


def test_manual_selection(snaps):
    rs = add_to_manual_selection(RuleSet(), "album_id", ["al1"])
    rs = add_to_manual_selection(rs, "album_id", ["al1", "al2"])
    rs = add_to_manual_selection(rs, "track_id", ["4"])
    manual = next(r for r in rs.rules if r.id == MANUAL_RULE_ID)
    assert manual.criteria == {
        "any": [{"in": {"album_id": ["al1", "al2"]}}, {"in": {"track_id": ["4"]}}]
    }
    assert sorted(evaluate_ruleset(rs, snaps).items) == ["lib/1", "lib/2", "lib/3", "lib/4"]
    rs = remove_from_manual_selection(rs, "album_id", ["al1"])
    assert sorted(evaluate_ruleset(rs, snaps).items) == ["lib/3", "lib/4"]


# --- benchmark ---------------------------------------------------------------------------------------


@pytest.mark.slow
def test_benchmark_100k_tracks():
    import random

    rnd = random.Random(1)
    genres = ["Rock", "Pop", "Jazz", "Metal", "Folk", "Electronic"]
    tracks = [
        track(
            str(i),
            artist=f"Artist {i % 3000}",
            album=f"Album {i % 9000}",
            album_id=f"al{i % 9000}",
            year=1950 + i % 75,
            genres=[rnd.choice(genres)],
            rating=rnd.randint(0, 5),
            play_count=rnd.randint(0, 50),
            starred=rnd.random() < 0.1,
            last_played=NOW if rnd.random() < 0.3 else None,
        )
        for i in range(100_000)
    ]
    snaps = {"lib": snapshot(tracks)}
    rs = RuleSet(
        rules=[
            Rule(
                id="r1",
                criteria={
                    "all": [
                        {"any": [{"is": {"loved": True}}, {"gt": {"rating": 3}}]},
                        {"inTheRange": {"year": [1970, 1999]}},
                        {"notContains": {"genre": "metal"}},
                    ]
                },
                sort="-rating,year,title",
                limit=5000,
            ),
            Rule(id="r2", criteria={"inTheLast": {"lastplayed": 30}}, sort="random", limit=2000),
            Rule(id="r3", criteria={"in": {"album_id": [f"al{i}" for i in range(0, 9000, 7)]}}),
        ],
        excludes=[Rule(id="x", criteria={"is": {"genre": "folk"}})],
    )
    # best of 3: other pytest workers on the Pi only ever make a run slower
    elapsed = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        sel = evaluate_ruleset(rs, snaps, now=NOW)
        elapsed = min(elapsed, time.perf_counter() - t0)
    assert sel.items
    assert elapsed < 2.0, f"evaluation took {elapsed:.2f}s"


def test_empty_text_value_warns():
    """`artist contains ""` (an untouched builder row) matches every item - valid, but warned about."""
    rs = RuleSet(
        rules=[
            Rule(
                id="r",
                criteria={"any": [{"contains": {"artist": ""}}, {"notContains": {"title": ""}}]},
            )
        ]
    )
    res = validate_ruleset(rs)
    assert res.ok
    assert any("'artist' has an empty value, so it matches every item" in w for w in res.warnings)
    assert any("'title' has an empty value, so it matches no item" in w for w in res.warnings)
    ok = validate_ruleset(RuleSet(rules=[Rule(id="r", criteria={"is": {"artist": ""}})]))
    assert ok.warnings == []
    # a blank is a real substring (" " matches "Pigeon John"), so it is not called empty
    blank = RuleSet(rules=[Rule(id="r", criteria={"contains": {"artist": "  "}})])
    assert validate_ruleset(blank).warnings == []


def test_in_and_not_in_lists():
    """`in` / `notIn` with a list of values, case-insensitive like `is`."""
    s = {
        "lib": snapshot(
            [
                track("1", artist="Linkin Park"),
                track("2", artist="limp bizkit"),
                track("3", artist="Korn"),
            ]
        )
    }
    assert sids(s, {"all": [{"in": {"artist": ["Linkin Park", "Limp Bizkit"]}}]}) == ["1", "2"]
    assert sids(s, {"all": [{"notIn": {"artist": ["LINKIN PARK", "Limp Bizkit"]}}]}) == ["3"]
    assert validate_rule(Rule(id="r", criteria={"all": [{"in": {"artist": "Korn"}}]})).errors


def test_in_lists_round_trip_through_nsp():
    """Exported as plain .nsp (any of `is` / all of `isNot`), parsed back into the list."""
    rule = Rule(
        id="x",
        criteria={
            "all": [
                {"in": {"artist": ["Linkin Park", "Limp Bizkit"]}},
                {"notIn": {"genre": ["Pop", "Disco"]}},
            ]
        },
    )
    doc, warnings = rule_to_nsp(rule)
    assert warnings == []
    assert doc == {
        "all": [
            {"any": [{"is": {"artist": "Linkin Park"}}, {"is": {"artist": "Limp Bizkit"}}]},
            {"all": [{"isNot": {"genre": "Pop"}}, {"isNot": {"genre": "Disco"}}]},
        ]
    }
    assert rule_from_nsp(doc, "x").criteria == rule.criteria
    # a whole rule of one list, and shapes that are not a list stay as they are
    only = rule_from_nsp({"any": [{"is": {"artist": "A"}}, {"is": {"artist": "B"}}]}, "y")
    assert only.criteria == {"all": [{"in": {"artist": ["A", "B"]}}]}
    mixed = {"any": [{"is": {"artist": "A"}}, {"is": {"album": "B"}}]}
    assert rule_from_nsp(mixed, "z").criteria == mixed
    single = {"any": [{"is": {"artist": "A"}}]}
    assert rule_from_nsp(single, "z").criteria == single
    loved = {"any": [{"is": {"loved": True}}, {"is": {"loved": False}}]}
    assert rule_from_nsp(loved, "z").criteria == loved

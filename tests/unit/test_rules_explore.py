"""Rule engine edge cases found by exploratory testing-10-17)."""

from __future__ import annotations

import contextlib
from datetime import timedelta

import pytest

from syncmusic.core.models import ItemRef, MediaKind, Playlist, Rule
from syncmusic.core.rules import RuleError, rule_from_nsp, rule_from_text, validate_rule
from syncmusic.core.rules.evaluator import EvalContext, evaluate_rule
from tests.factories import NOW, snapshot, track


@pytest.fixture
def snaps():
    pl = Playlist(
        ref=ItemRef(library_id="lib", item_id="pl1"),
        name="Улюблене",
        track_refs=[ItemRef(library_id="lib", item_id="a")],
    )
    tracks = [
        track(
            "a", title="Ялинка", artist="Скрябін", genres=["Рок"], year=1988, size_bytes=10_000_000
        ),
        track("b", title="ялинка 2", artist="скрябін", genres=["рок"], size_bytes=20_000_000),
        track("c", title="Strasse", artist="Rammstein", album="Straße", year=2001),
        track("d", kind=MediaKind.PODCAST, series="P", published=NOW - timedelta(days=1)),
        track("e", kind=MediaKind.PODCAST, series="P", date_added=NOW),
    ]
    return {"lib": snapshot(tracks, playlists=[pl])}


def ids(snaps, criteria, **kw):
    return sorted(
        t.ref.item_id
        for t in evaluate_rule(
            Rule(id="r", criteria=criteria, **kw), EvalContext(snaps, NOW)
        ).tracks
    )


def test_cyrillic_case_insensitive(snaps):
    assert ids(snaps, {"contains": {"title": "ЯЛИНКА"}}) == ["a", "b"]
    assert ids(snaps, {"is": {"artist": "СКРЯБІН"}}) == ["a", "b"]
    assert ids(snaps, {"contains": {"genre": "РОК"}}) == ["a", "b"]
    assert ids(snaps, {"contains": {"album": "strasse"}}) == ["c"]  # ß casefolds to ss
    assert ids(snaps, {"inPlaylist": {"name": "УЛЮБЛЕНЕ"}}) == ["a"]
    assert ids(snaps, {"IS": {"TITLE": "ялинка"}}) == ["a"]


def test_latest_episodes_falls_back_to_dateadded(snaps):
    assert ids(snaps, {"latestEpisodes": {"per_series": 1}}) == ["e"]


def test_random_sort_is_stable_across_runs(snaps):
    first = [
        t.ref.key()
        for t in evaluate_rule(Rule(id="r", sort="random", limit=3), EvalContext(snaps, NOW)).tracks
    ]
    for _ in range(3):
        again = evaluate_rule(Rule(id="r", sort="random", limit=3), EvalContext(snaps, NOW))
        assert [t.ref.key() for t in again.tracks] == first


@pytest.mark.parametrize(
    "kw",
    [
        {"limit": -1},
        {"limit_percent": 0},
        {"limit_percent": 150},
        {"limit_duration": "abc"},
        {"limit_size": "lots"},
    ],
)
def test_invalid_limits_rejected(kw):
    with contextlib.suppress(RuleError):  # either a RuleError or a failed validation is fine
        assert not validate_rule(Rule(id="r", **kw)).ok


def test_unknown_field_warns_not_crash(snaps):
    res = evaluate_rule(Rule(id="r", criteria={"is": {"nosuch": 1}}), EvalContext(snaps, NOW))
    assert res.tracks == [] and res.warnings


def test_text_parse_errors():
    assert rule_from_nsp('{"name": "x", // inline\n "all": []}').name == "x"
    assert rule_from_nsp('{"name": "x", /* block */ "all": []}').name == "x"
    for bad in ("not: [valid", '{"id": 5}', "id: r\ncriteria: {}\nlimt: 5"):
        with pytest.raises(RuleError):
            rule_from_text(bad)
    assert not validate_rule(Rule(id="r", criteria={"is": {"loved": "yes"}})).ok


def test_empty_any_matches_nothing(snaps):
    assert ids(snaps, {"any": []}) == []
    assert ids(snaps, {"all": [{"any": []}, {"is": {"artist": "скрябін"}}]}) == []
    assert len(ids(snaps, {"all": []})) == 5  # empty all / {} keep matching everything

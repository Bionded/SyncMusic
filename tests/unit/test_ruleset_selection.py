"""evaluate_ruleset: excludes, media kinds and which playlists go to the device."""

from syncmusic.core.models import ItemRef, MediaKind, Playlist, Rule, RuleSet
from syncmusic.core.rules.evaluator import evaluate_ruleset
from tests.factories import NOW, snapshot, track


def _pl(lib: str, pid: str, name: str, *ids: str, smart: bool = False) -> Playlist:
    return Playlist(
        ref=ItemRef(library_id=lib, item_id=pid),
        name=name,
        track_refs=[ItemRef(library_id=lib, item_id=i) for i in ids],
        smart=smart,
    )


def _snaps():
    a = snapshot(
        [track("1", "A"), track("2", "A"), track("p", "A", kind=MediaKind.PODCAST)],
        "A",
        [_pl("A", "pl1", "Mix", "1", "2", "9"), _pl("A", "pl2", "Road", "2", smart=True)],
    )
    b = snapshot(
        [track("3", "B")], "B", [_pl("B", "plb", "mix", "3"), _pl("B", "plc", "Other", "3")]
    )
    return {"A": a, "B": b}


def _names(sel) -> list[str]:
    return [p.name for p in sel.playlists]


def test_excludes_warnings_and_kinds() -> None:
    rs = RuleSet(
        rules=[Rule(id="all", criteria={"all": []})],
        excludes=[
            Rule(id="x-off", enabled=False, criteria={"is": {"title": "Song 1"}}),
            Rule(id="x2", name="no two", criteria={"is": {"title": "Song 2"}}),
            Rule(id="xg", criteria={"inPlaylist": {"id": "gone"}}),
        ],
    )
    sel = evaluate_ruleset(rs, _snaps(), allowed_kinds={MediaKind.MUSIC}, now=NOW)
    assert sorted(sel.items) == [
        "A/1",
        "B/3",
    ]  # disabled exclude ignored, the later rules still run
    assert sel.excluded == {"A/2": "x2"}
    assert any(w.startswith("exclude xg: ") for w in sel.warnings)
    assert "1 selected items skipped: media kind not supported by device" in sel.warnings


def test_skipped_kinds_counted_per_item() -> None:
    snaps = {
        "A": snapshot(
            [track(str(i), "A", kind=MediaKind.PODCAST) for i in range(3)] + [track("m", "A")], "A"
        )
    }
    sel = evaluate_ruleset(
        RuleSet(rules=[Rule(id="r")]), snaps, allowed_kinds={MediaKind.MUSIC}, now=NOW
    )
    assert list(sel.items) == ["A/m"]
    assert "3 selected items skipped: media kind not supported by device" in sel.warnings


def test_rule_warnings_named() -> None:
    rs = RuleSet(rules=[Rule(id="r1", criteria={"inPlaylist": {"id": "gone"}})])
    sel = evaluate_ruleset(rs, _snaps(), now=NOW)
    assert sel.warnings and all(w.startswith("rule r1: ") for w in sel.warnings)


def test_all_playlists_of_the_rule_libraries() -> None:
    sel = evaluate_ruleset(RuleSet(rules=[Rule(id="r", include_playlists=True)]), _snaps(), now=NOW)
    # name clash across libraries: the first one wins (case-insensitive)
    assert _names(sel) == ["Mix", "Road", "Other"]
    mix = sel.playlists[0]
    assert mix.source == ItemRef(library_id="A", item_id="pl1") and not mix.smart
    assert [r.key() for r in mix.refs] == ["A/1", "A/2"]  # members not on the device are dropped
    assert sel.playlists[1].smart

    only_b = evaluate_ruleset(
        RuleSet(rules=[Rule(id="r", libraries=["B"], include_playlists=True)]), _snaps(), now=NOW
    )
    assert _names(only_b) == ["mix", "Other"]
    # an attached library without a snapshot is skipped, the rest still go
    gone = evaluate_ruleset(
        RuleSet(rules=[Rule(id="r", include_playlists=True)]),
        _snaps(),
        attached=["Z", "B"],
        now=NOW,
    )
    assert _names(gone) == ["mix", "Other"]


def test_in_playlist_refs_pick_playlists() -> None:
    def names(criteria) -> list[str]:
        return _names(
            evaluate_ruleset(RuleSet(rules=[Rule(id="r", criteria=criteria)]), _snaps(), now=NOW)
        )

    assert names({"inPlaylist": {"id": "pl2"}}) == ["Road"]
    assert names({"inPlaylist": {"id": "B/plc"}}) == ["Other"]  # ref key form
    assert names({"inPlaylist": {"name": "OTHER"}}) == ["Other"]
    assert names({"any": [{"inPlaylist": {"id": "pl2"}}, {"inPlaylist": {"name": "other"}}]}) == [
        "Road",
        "Other",
    ]
    assert names({"all": []}) == []  # no inPlaylist, no include_playlists
    skip = RuleSet(
        rules=[Rule(id="r", skip_playlists=True, criteria={"inPlaylist": {"id": "pl2"}})]
    )
    assert _names(evaluate_ruleset(skip, _snaps(), now=NOW)) == []


def test_as_playlist_blocks_same_named_library_playlist() -> None:
    rs = RuleSet(rules=[Rule(id="r", as_playlist="MIX", include_playlists=True)])
    sel = evaluate_ruleset(rs, _snaps(), now=NOW)
    assert _names(sel) == ["MIX", "Road", "Other"]
    assert sel.playlists[0].source is None

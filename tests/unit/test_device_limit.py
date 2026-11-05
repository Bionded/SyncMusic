"""A limit for the whole device with a fill order: at most N bytes / N tracks, filled by priority."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.core.models import (
    DeviceConfig,
    Manifest,
    ManifestEntry,
    ManifestHeader,
    Rule,
    RuleSet,
)
from syncmusic.core.planner import meta_hash, plan_sync
from syncmusic.core.prepare import Chain, initial_plan_item
from tests.factories import NOW, snapshot, track

CAPS = DeviceCapabilities(formats={"mp3"}, supports_podcasts=False)
ALL = Rule(id="all", criteria={"all": []})
DAY = timedelta(days=1)
MB = 1_000_000


def _tracks():
    # ids sort "1".."6" in selection order (artist, title): the priority order when nothing else decides
    return [
        track("1", artist="A", rating=1, play_count=50, last_played=NOW - 300 * DAY),
        track("2", artist="B", rating=5, play_count=1, last_played=NOW - 1 * DAY),
        track("3", artist="C", rating=3, play_count=20, date_added=NOW - 2 * DAY),
        track("4", artist="D", rating=4, play_count=0, date_added=NOW - 900 * DAY),
        track("5", artist="E", rating=None, play_count=7, last_played=NOW - 30 * DAY),
        track("6", artist="F", rating=2, play_count=3, date_added=NOW - 50 * DAY),
    ]


def _device(dev_id="dev", rules=None, **rs) -> DeviceConfig:
    return DeviceConfig(
        id=dev_id,
        type="fake",
        name="D",
        library_ids=["lib"],
        rule_set=RuleSet(rules=rules or [ALL], **rs),
    )


def _plan(device, tracks=None, manifest=None):
    return plan_sync(
        device, {"lib": snapshot(tracks or _tracks())}, manifest, CAPS, Chain([]), now=NOW
    )


def _on_device(plan) -> set[str]:
    return {
        a.ref.item_id for a in plan.actions if a.kind in ("add", "replace", "keep", "update_meta")
    }


def test_no_limit_takes_everything():
    plan = _plan(_device())
    assert _on_device(plan) == {"1", "2", "3", "4", "5", "6"} and plan.limit is None


def test_count_limit_by_priority_keeps_the_selection_order():
    plan = _plan(_device(limit_tracks=2))
    assert _on_device(plan) == {"1", "2"}
    assert plan.limit is not None and plan.limit.kept_items == 2
    assert [x.key for x in plan.limit.left_out] == ["lib/3", "lib/4", "lib/5", "lib/6"]
    assert plan.limit.left_out[0].title == "C – Song 3" and plan.limit.left_out_bytes == 4 * 5 * MB
    assert any(
        "4 selected items (20.0 MB) left out by the device limit (2 tracks" in w
        for w in plan.warnings
    )


def test_size_limit_counts_device_bytes_and_stops_at_the_first_item_that_does_not_fit():
    tracks = _tracks()
    tracks[1] = tracks[1].model_copy(update={"size_bytes": 12 * MB})  # "2" does not fit after "1"
    plan = _plan(_device(limit_bytes=15 * MB), tracks)
    # strict order: the small "3" does not jump over "2"
    assert _on_device(plan) == {"1"}
    assert plan.limit.kept_bytes == 5 * MB and plan.limit.limit_bytes == 15 * MB


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        (["rating"], {"2", "4", "3"}),
        (["play_count"], {"1", "3", "5"}),
        (["recently_played"], {"2", "5", "1"}),
        (["recently_added"], {"3", "6", "4"}),
        (["priority"], {"1", "2", "3"}),
    ],
)
def test_each_fill_order(order, expected):
    plan = _plan(_device(limit_tracks=3, fill_order=order))
    assert _on_device(plan) == expected
    assert plan.limit.fill_order == order


def test_fill_orders_combine_ties_fall_back_to_the_next_key():
    tracks = [t.model_copy(update={"rating": 3}) for t in _tracks()]
    tracks[5] = tracks[5].model_copy(update={"rating": 5})
    plan = _plan(_device(limit_tracks=2, fill_order=["rating", "play_count"]), tracks)
    assert _on_device(plan) == {
        "6",
        "1",
    }  # 6 has the best rating, then the most played of the 3-star ones


def test_unrated_and_never_played_items_come_last():
    plan = _plan(_device(limit_tracks=5, fill_order=["rating"]))
    assert _on_device(plan) == {"1", "2", "3", "4", "6"}  # "5" has no rating
    plan = _plan(_device(limit_tracks=3, fill_order=["recently_played"]))
    assert _on_device(plan) == {"1", "2", "5"}


def test_random_is_stable_per_device_and_differs_between_devices():
    tracks = [track(str(i), artist=f"A{i:03}") for i in range(60)]
    picks = [
        _on_device(_plan(_device(limit_tracks=10, fill_order=["random"]), tracks)) for _ in range(3)
    ]
    assert picks[0] == picks[1] == picks[2] and len(picks[0]) == 10
    assert picks[0] != {str(i) for i in range(10)}  # not simply the priority order
    other = _on_device(_plan(_device("other", limit_tracks=10, fill_order=["random"]), tracks))
    assert other != picks[0]


def _manifest(tracks, ids) -> Manifest:
    chain = Chain([])
    entries = [
        ManifestEntry(
            ref=t.ref,
            device_ref=f"Music/{t.title}.mp3",
            path=f"Music/{t.title}.mp3",
            size=t.size_bytes,
            source_state=t.source_state,
            chain_hash=chain.chain_hash(initial_plan_item(t), CAPS),
            meta_hash=meta_hash(t),
            rules=["all"],
        )
        for t in tracks
        if t.ref.item_id in ids
    ]
    return Manifest(header=ManifestHeader(device_id="dev"), items={e.ref.key(): e for e in entries})


def test_mirror_removes_what_no_longer_fits_with_the_reason():
    tracks = _tracks()
    plan = _plan(
        _device(limit_tracks=2, fill_order=["rating"]), tracks, _manifest(tracks, {"1", "2"})
    )
    kinds = {a.ref.item_id: a for a in plan.actions}
    assert kinds["1"].kind == "remove" and kinds["1"].reason == "over the device limit"
    assert kinds["2"].kind == "keep" and kinds["4"].kind == "add"


def test_additive_fills_around_what_is_already_on_the_device():
    tracks = _tracks()
    plan = _plan(
        _device(limit_tracks=3, fill_order=["rating"], mode="additive"),
        tracks,
        _manifest(tracks, {"1", "6"}),
    )
    # 1 and 6 stay (additive never removes) and count against the limit: one place left, the best rated
    assert _on_device(plan) == {"1", "6", "2"}
    assert {x.key for x in plan.limit.left_out} == {"lib/3", "lib/4", "lib/5"}


def test_playlists_lose_the_left_out_items():
    from syncmusic.core.models import ItemRef, Playlist

    caps = CAPS.model_copy(update={"supports_playlists": True})
    pl = Playlist(
        ref=ItemRef(library_id="lib", item_id="p"), name="P", track_refs=[t.ref for t in _tracks()]
    )
    rule = Rule(id="all", criteria={"all": []}, include_playlists=True)
    snap = snapshot(_tracks(), playlists=[pl])
    plan = plan_sync(
        _device(rules=[rule], limit_tracks=2), {"lib": snap}, None, caps, Chain([]), now=NOW
    )
    assert [r.item_id for r in plan.playlists[0].refs] == ["1", "2"]


def test_limits_are_validated():
    with pytest.raises(ValidationError):
        RuleSet(limit_tracks=0)
    with pytest.raises(ValidationError):
        RuleSet(fill_order=["loudness"])
    rs = RuleSet.model_validate({"limit_bytes": 50_000_000_000, "fill_order": ["random"]})
    assert rs.limit_bytes == 50_000_000_000

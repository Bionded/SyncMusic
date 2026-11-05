"""Three-way playlist merge."""

from __future__ import annotations

from syncmusic.core.playlist_merge import merge, reordered

BASE = ["a", "b", "c", "d"]


def test_one_side_changed_wins():
    assert merge(BASE, BASE, ["a", "c"]) == ["a", "c"]
    assert merge(BASE, ["d", "c", "b", "a"], BASE) == ["d", "c", "b", "a"]
    assert merge(BASE, ["x"], ["x"]) == ["x"]


def test_union_of_adds_and_removes_from_both():
    # device removed b and added x after c; library removed d and added y at the start
    assert merge(BASE, ["a", "c", "x", "d"], ["y", "a", "b", "c"]) == ["y", "a", "c", "x"]


def test_order_of_the_side_that_reordered():
    # device reordered (d first), library removed c and added e at the end
    assert merge(BASE, ["d", "a", "b", "c"], ["a", "b", "d", "e"]) == ["d", "a", "b", "e"]


def test_both_reordered_is_a_conflict():
    assert merge(BASE, ["b", "a", "c", "d"], ["a", "b", "d", "c"]) is None


def test_duplicates_counted():
    assert merge(["a", "a"], ["a"], ["a", "a", "b"]) == ["a", "b"]
    assert not reordered(["a", "b"], ["a", "x", "b"])
    assert reordered(["a", "b"], ["b", "a"])


def test_manifest_keeps_the_merge_base():
    from syncmusic.core import manifest as mf
    from syncmusic.core.models import Manifest, ManifestHeader

    m = Manifest(
        header=ManifestHeader(device_id="d"),
        playlists=["Mix", "Smart"],
        playlist_refs={"Mix": ["nd/1", "nd/2", "nd/1"], "Smart": []},
        playlist_sources={"Mix": "nd/pl1", "Smart": "nd/pl2"},
        playlist_smart={"Smart"},
    )
    back = mf.loads(mf.dumps(m))
    assert back.playlist_refs == m.playlist_refs
    assert back.playlist_sources == m.playlist_sources and back.playlist_smart == {"Smart"}


def test_preferred_side_resolves_a_double_reorder():
    dev, lib = ["b", "a", "c", "d", "x"], ["a", "b", "d", "c"]
    assert merge(BASE, dev, lib, prefer="device") == ["b", "a", "c", "d", "x"]
    assert merge(BASE, dev, lib, prefer="library") == ["a", "b", "d", "c", "x"]

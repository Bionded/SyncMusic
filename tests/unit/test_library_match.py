"""Matching a library another instance synced from against ours: source id, stored alias, probe items."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from syncmusic.core.library_match import ForeignLibrary, ProbeItem, match_library, probe_items
from syncmusic.core.models import LibraryConfig, LibrarySnapshot
from tests.factories import track
from tests.unit.test_api_sync import env, music  # noqa: F401 - fixtures


def _cfg(lid: str, source_id: str | None = None, type_: str = "subsonic", **kw) -> LibraryConfig:
    return LibraryConfig(id=lid, type=type_, name=lid, source_id=source_id, **kw)


def _snap(lid: str, n: int = 5, title: str = "Пісня") -> LibrarySnapshot:
    tracks = {
        f"t{i}": track(f"t{i}", lid, title=f"{title} {i}", duration_s=200.0 + i) for i in range(n)
    }
    return LibrarySnapshot(library_id=lid, state_token="x", tracks=tracks)


def _foreign(source_id: str | None = "subsonic:abc", **kw) -> ForeignLibrary:
    probe = probe_items(_snap("theirs"), [f"t{i}" for i in range(5)])
    return ForeignLibrary(source_id=source_id, type="subsonic", name="Navidrome", probe=probe, **kw)


def test_equal_source_id_matches():
    m = match_library(_foreign(), [_cfg("a", "subsonic:zzz"), _cfg("b", "subsonic:abc")], {}, {})
    assert m is not None and (m.library_id, m.how, m.confirm) == ("b", "source_id", False)
    moved = _cfg("c", "subsonic:old", source_id_now="subsonic:abc")
    assert match_library(_foreign(), [moved], {}, {}).library_id == "c"  # type: ignore[union-attr]


def test_stored_alias_matches_without_probing():
    m = match_library(_foreign(), [_cfg("nd", "subsonic:drifted")], {}, {"subsonic:abc": "nd"})
    assert m is not None and (m.how, m.confirm) == ("alias", False)
    assert match_library(_foreign(), [_cfg("x")], {}, {"subsonic:abc": "gone"}) is None


def test_drifted_fingerprint_falls_back_to_the_probe():
    m = match_library(_foreign(), [_cfg("nd", "subsonic:drifted")], {"nd": _snap("nd")}, {})
    assert m is not None and (m.library_id, m.how, m.confirm) == ("nd", "probe", True)


def test_probe_needs_four_of_five_with_same_title_and_duration():
    snap = _snap("nd")
    snap.tracks["t0"] = track("t0", "nd", title="Інша", duration_s=200.0)
    assert match_library(_foreign(), [_cfg("nd")], {"nd": snap}, {}) is not None  # 4 of 5
    snap.tracks["t1"] = track("t1", "nd", title="Пісня 1", duration_s=300.0)  # duration off
    assert match_library(_foreign(), [_cfg("nd")], {"nd": snap}, {}) is None
    other_type = _cfg("f", type_="folder")
    assert match_library(_foreign(), [other_type], {"f": _snap("f")}, {}) is None


def test_two_equal_candidates_are_not_guessed():
    libs, snaps = [_cfg("a"), _cfg("b")], {"a": _snap("a"), "b": _snap("b")}
    assert match_library(_foreign(), libs, snaps, {}) is None


def test_unknown_source_and_no_probe_is_unmatched():
    assert (
        match_library(ForeignLibrary(type="subsonic"), [_cfg("nd")], {"nd": _snap("nd")}, {})
        is None
    )


def test_probe_items_spread_and_skip_unknown_ids():
    snap = _snap("nd", n=20)
    items = probe_items(snap, ["missing", *[f"t{i}" for i in range(20)]])
    assert [p.item_id for p in items] == ["t0", "t4", "t8", "t12", "t16"]
    assert items[0] == ProbeItem(item_id="t0", title="Пісня 0", duration_s=200.0)


async def test_aliases_are_stored(env):
    app, _ = env
    assert await app.store.library_aliases() == {}
    await app.store.put_library_alias("subsonic:abc", "music")
    await app.store.put_library_alias("abs:x", "music")
    await app.store.put_library_alias("abs:x", None)
    assert await app.store.library_aliases() == {"subsonic:abc": "music"}
    await app.store.app_settings()  # an extra settings row does not break the app settings

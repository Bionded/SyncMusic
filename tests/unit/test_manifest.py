"""On-device manifest and inflight list."""

from __future__ import annotations

import json
import os

import pytest

from syncmusic.core import manifest as mf
from syncmusic.core.models import ItemRef, Manifest, ManifestEntry, ManifestHeader, ManifestLibrary


def _manifest(n: int = 3) -> Manifest:
    items = {}
    for i in range(n):
        ref = ItemRef(library_id="nav", item_id=f"id{i}")
        path = f"Music/Тартак/Гранатовий альбом/{i:02d} - Орбіт без цукру.mp3"
        items[ref.key()] = ManifestEntry(
            ref=ref, device_ref=path, path=path, size=1000 + i, rules=["r1"], last_rating=4
        )
    return Manifest(
        header=ManifestHeader(device_id="dev1", app_version="0.1"), items=items, playlists=["Мій"]
    )


def test_round_trip_cyrillic(tmp_path):
    m = _manifest()
    mf.write(tmp_path, m)
    text = (tmp_path / mf.MANIFEST_PATH).read_text(encoding="utf-8")
    assert "Тартак" in text  # readable UTF-8, not \u escapes in the playlist line either
    assert "Мій" in text
    assert json.loads(text.splitlines()[0])["type"] == "header"
    got = mf.read(tmp_path)
    assert got == m


def test_no_manifest_is_none(tmp_path):
    assert mf.read(tmp_path) is None


def test_atomic_write_keeps_old_file_on_failure(tmp_path, monkeypatch):
    old = _manifest(1)
    mf.write(tmp_path, old)

    def boom(fd):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        mf.write(tmp_path, _manifest(5))
    monkeypatch.undo()
    assert mf.read(tmp_path) == old
    assert not list((tmp_path / mf.STATE_DIR).glob("*.tmp"))


def test_newer_version_raises_with_choices(tmp_path):
    m = _manifest(1)
    text = mf.dumps(m).replace('"manifest_version":2', '"manifest_version":99', 1)
    (tmp_path / mf.STATE_DIR).mkdir()
    (tmp_path / mf.MANIFEST_PATH).write_text(text)
    with pytest.raises(mf.ManifestTooNew) as e:
        mf.read(tmp_path)
    assert e.value.found == 99 and set(e.value.choices) == {"wipe", "try_anyway"}
    assert mf.read(tmp_path, allow_newer=True).items.keys() == m.items.keys()


def test_newer_version_with_unknown_fields_raises_too_new_even_if_trying(tmp_path):
    text = '{"type":"header","manifest_version":3,"device_id":"d"}\n{"type":"item","ref":42}\n'
    with pytest.raises(mf.ManifestTooNew):
        mf.loads(text, allow_newer=True)


def test_truncated_last_line_raises(tmp_path):
    text = mf.dumps(_manifest(3))
    with pytest.raises(mf.ManifestError, match="not valid JSON"):
        mf.loads(text[: len(text) - 40])


@pytest.mark.parametrize("text", ["", "\n", '{"type":"item"}\n', "[1]\n", '{"type":"header"}\n'])
def test_garbage_raises(text):
    with pytest.raises(mf.ManifestError):
        mf.loads(text)


def test_invalid_utf8_raises(tmp_path):
    (tmp_path / mf.STATE_DIR).mkdir()
    (tmp_path / mf.MANIFEST_PATH).write_bytes(b"\xff\xfe garbage")
    with pytest.raises(mf.ManifestError):
        mf.read(tmp_path)


def test_unknown_line_type_skipped():
    text = mf.dumps(_manifest(2)) + '{"type":"future_thing","x":1}\n'
    assert len(mf.loads(text).items) == 2


def test_inflight_add_clear_and_clean(tmp_path):
    assert mf.read_inflight(tmp_path) == []
    (tmp_path / "Music").mkdir()
    for name in ("a.mp3", "b.mp3", "kept.mp3"):
        (tmp_path / "Music" / name).write_bytes(b"x")
    mf.add_inflight(tmp_path, ["Music/a.mp3", "Music/kept.mp3"])
    mf.add_inflight(tmp_path, ["Music/b.mp3", "../outside.mp3", "Music/missing.mp3"])
    assert mf.read_inflight(tmp_path)[:3] == ["Music/a.mp3", "Music/kept.mp3", "Music/b.mp3"]
    (tmp_path.parent / "outside.mp3").write_bytes(b"x")
    ref = ItemRef(library_id="l", item_id="k")
    m = Manifest(
        header=ManifestHeader(device_id="d"),
        items={
            ref.key(): ManifestEntry(
                ref=ref, device_ref="Music/kept.mp3", path="Music/kept.mp3", size=1
            )
        },
    )
    removed = mf.clean_inflight(tmp_path, m)
    assert removed == ["Music/a.mp3", "Music/b.mp3"]
    assert (tmp_path / "Music" / "kept.mp3").exists()
    assert (tmp_path.parent / "outside.mp3").exists()  # never outside the device root
    assert mf.read_inflight(tmp_path) == []
    (tmp_path.parent / "outside.mp3").unlink()


def test_read_header_reads_only_the_first_line(tmp_path, monkeypatch):
    """matching a device needs only the header: a big manifest's entries are never parsed."""
    m = _manifest(20_000)
    m.header.device_uid = "uid-1"
    mf.write(tmp_path, m)

    def no_parse(*a, **kw):
        raise AssertionError("entries parsed")

    monkeypatch.setattr(mf, "loads", no_parse)
    monkeypatch.setattr(ManifestEntry, "model_validate", no_parse)
    header = mf.read_header(tmp_path)
    assert header is not None and header.device_uid == "uid-1" and header.device_id == "dev1"
    assert mf.read_header(tmp_path / "none") is None
    (tmp_path / mf.MANIFEST_PATH).write_text('{"type": "item"}\n', encoding="utf-8")
    with pytest.raises(mf.ManifestError):
        mf.read_header(tmp_path)


def test_second_read_is_served_from_the_cache(tmp_path, monkeypatch):
    mf.write(tmp_path, _manifest())
    first = mf.read(tmp_path)
    parses = []
    real = mf.loads
    monkeypatch.setattr(mf, "loads", lambda *a, **kw: parses.append(1) or real(*a, **kw))
    again = mf.read(tmp_path)
    assert parses == [] and again == first


def test_cached_copy_is_not_changed_by_callers(tmp_path):
    mf.write(tmp_path, _manifest())
    got = mf.read(tmp_path)
    key = next(iter(got.items))
    got.items[key].last_playcount = 99
    got.items.pop(next(iter(reversed(got.items))))
    got.header.interrupted = None
    got.playlists.append("x")
    fresh = mf.read(tmp_path)
    assert len(fresh.items) == 3 and fresh.items[key].last_playcount == 0
    assert fresh.playlists == ["Мій"]


def test_cache_dropped_on_write_and_delete(tmp_path):
    mf.write(tmp_path, _manifest(3))
    assert len(mf.read(tmp_path).items) == 3
    mf.write(tmp_path, _manifest(2))
    assert len(mf.read(tmp_path).items) == 2
    mf.delete(tmp_path)
    assert mf.read(tmp_path) is None


def test_change_made_elsewhere_is_seen(tmp_path):
    """another host (or a tool) rewrote the manifest: same size and mtime, but the header line differs."""
    mf.write(tmp_path, _manifest(3))
    path = tmp_path / mf.MANIFEST_PATH
    st = path.stat()
    assert len(mf.read(tmp_path).items) == 3
    other = _manifest(3)
    other.header.device_id = other.header.device_id[::-1]  # same length
    path.write_text(mf.dumps(other), encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert mf.read(tmp_path).header.device_id == other.header.device_id


# --- version 2: library keys -----------------------------------------------------------------

A = mf.Context(
    instance_id="inst-a",
    instance_name="alpha",
    libraries={"nav": ManifestLibrary(source_id="subsonic:f00", type="subsonic", name="Music")},
)


def _with_playlist(m: Manifest) -> Manifest:
    m.playlist_refs["Мій"] = ["nav/id0", "nav/id1"]
    m.playlist_sources["Мій"] = "nav/pl1"
    return m


def test_v2_entries_name_header_keys_not_local_ids():
    m = _with_playlist(_manifest(2))
    text = mf.dumps(m, A)
    rows = [json.loads(line) for line in text.splitlines()]
    head = rows[0]
    assert head["manifest_version"] == 2
    lib = head["libraries"]["L1"]
    assert lib["id"] == "nav" and lib["source_id"] == "subsonic:f00" and lib["instance"] == "inst-a"
    assert [p["item_id"] for p in lib["probe"]] == ["id0", "id1"]
    assert head["home"]["id"] == head["last_writer"]["id"] == "inst-a"
    item = rows[1]
    assert item["lib"] == "L1" and item["item_id"] == "id0" and "ref" not in item
    pl = rows[3]
    assert pl["refs"] == ["L1/id0", "L1/id1"] and pl["source"] == "L1/pl1"
    assert '"nav' not in text.split("\n", 1)[1]  # local ids stay in the header table only


def test_v2_round_trip_same_instance():
    m = _with_playlist(_manifest(2))
    got = mf.loads(mf.dumps(m, A), ctx=A)
    assert got == m
    assert set(got.items) == {"nav/id0", "nav/id1"} and got.playlist_sources["Мій"] == "nav/pl1"


def test_other_instance_matches_by_source_id_or_alias_else_ext():
    text = mf.dumps(_with_playlist(_manifest(2)), A)
    same_source = mf.Context(
        instance_id="inst-b", libraries={"music": ManifestLibrary(source_id="subsonic:f00")}
    )
    assert set(mf.loads(text, ctx=same_source).items) == {"music/id0", "music/id1"}
    aliased = mf.Context(
        instance_id="inst-b",
        libraries={"mine": ManifestLibrary(source_id="subsonic:other")},
        aliases={"subsonic:f00": "mine"},
    )
    assert set(mf.loads(text, ctx=aliased).items) == {"mine/id0", "mine/id1"}
    stranger = mf.Context(instance_id="inst-b", libraries={"nav": ManifestLibrary(source_id="x")})
    got = mf.loads(text, ctx=stranger)
    assert set(got.items) == {"ext:L1/id0", "ext:L1/id1"}
    assert all(mf.is_external(e.ref.library_id) for e in got.items.values())
    assert got.playlist_refs["Мій"] == ["ext:L1/id0", "ext:L1/id1"]


def test_ext_library_survives_a_write_by_another_instance():
    text = mf.dumps(_manifest(2), A)
    b = mf.Context(
        instance_id="inst-b",
        instance_name="beta",
        libraries={"folder": ManifestLibrary(source_id="folder:u:/m", type="folder")},
    )
    m = mf.loads(text, ctx=b)
    ref = ItemRef(library_id="folder", item_id="f1")
    m.items[ref.key()] = ManifestEntry(ref=ref, device_ref="x.mp3", path="x.mp3", size=5)
    head = json.loads(mf.dumps(m, b).splitlines()[0])
    assert head["libraries"]["L1"]["instance"] == "inst-a"  # kept as read
    assert (
        head["libraries"]["L2"]["id"] == "folder"
        and head["libraries"]["L2"]["instance"] == "inst-b"
    )
    assert head["home"]["id"] == "inst-a" and head["last_writer"]["name"] == "beta"
    back = mf.loads(mf.dumps(m, b), ctx=A)
    assert set(back.items) == {"nav/id0", "nav/id1", "ext:L2/f1"}


def test_v1_manifest_read_and_upgraded_on_write(tmp_path):
    m = _manifest(2)
    v1 = [json.loads(mf.dumps(m, A).splitlines()[0])]
    v1[0].update(manifest_version=1, libraries={}, home=None, last_writer=None)
    v1 += [{**json.loads(e.model_dump_json())} for e in m.items.values()]
    (tmp_path / mf.STATE_DIR).mkdir()
    (tmp_path / mf.MANIFEST_PATH).write_text("\n".join(json.dumps(r) for r in v1) + "\n")
    got = mf.read(tmp_path)
    assert set(got.items) == {"nav/id0", "nav/id1"}
    mf.set_context(A)
    try:
        mf.write(tmp_path, got)
        head = json.loads((tmp_path / mf.MANIFEST_PATH).read_text().splitlines()[0])
    finally:
        mf.set_context(mf.Context())
    assert head["manifest_version"] == 2 and head["libraries"]["L1"]["id"] == "nav"


def test_unchanged_ignores_only_updated_at(tmp_path):
    m = _manifest(2)
    mf.write(tmp_path, m)
    before = (tmp_path / mf.MANIFEST_PATH).stat().st_mtime_ns
    again = mf.read(tmp_path)
    again.header = again.header.model_copy(
        update={"updated_at": again.header.updated_at.replace(year=2030)}
    )
    assert mf.unchanged((tmp_path / mf.MANIFEST_PATH).read_bytes(), again)
    mf.write(tmp_path, again)
    assert (tmp_path / mf.MANIFEST_PATH).stat().st_mtime_ns == before


def test_context_change_invalidates_cached_parse(tmp_path):
    mf.write(tmp_path, _manifest(1))
    assert set(mf.read(tmp_path).items) == {"nav/id0"}
    mf.set_context(mf.Context(instance_id="other", libraries={"x": ManifestLibrary()}))
    try:
        assert set(mf.read(tmp_path).items) == {"ext:L1/id0"}
    finally:
        mf.set_context(mf.Context())


def test_own_removed_library_stays_ours_not_ext():
    text = mf.dumps(_manifest(1), A)
    without = mf.Context(instance_id="inst-a", libraries={"other": ManifestLibrary()})
    assert set(mf.loads(text, ctx=without).items) == {"nav/id0"}


def _lines(root):
    return (root / mf.MANIFEST_PATH).read_text(encoding="utf-8").splitlines()


def _fresh_read(root):
    mf.drop_cache()
    return mf.read(root)


def test_item_changes_are_appended_and_read_back_like_a_full_write(tmp_path):
    m = _manifest(400)
    mf.write(tmp_path, m)
    base = len(_lines(tmp_path))
    m = mf.read(tmp_path)
    m.items.pop("nav/id1")
    m.items["nav/id2"] = m.items["nav/id2"].model_copy(update={"size": 5})
    ref = ItemRef(library_id="nav", item_id="new")
    m.items[ref.key()] = ManifestEntry(ref=ref, device_ref="a.mp3", path="a.mp3", size=1)
    mf.write(tmp_path, m)
    lines = _lines(tmp_path)
    assert len(lines) == base + 4  # marker, two items, one delete
    assert json.loads(lines[base])["type"] == "append"
    got = _fresh_read(tmp_path)
    assert got.items == m.items
    assert got.playlists == m.playlists
    full = tmp_path / "full"
    mf.write(full, m, compact=True)
    assert _fresh_read(full).items == got.items


def test_torn_appended_line_is_dropped_and_forces_compaction(tmp_path):
    m = _manifest(400)
    mf.write(tmp_path, m)
    m = mf.read(tmp_path)
    m.items["nav/id3"] = m.items["nav/id3"].model_copy(update={"size": 7})
    mf.write(tmp_path, m)
    path = tmp_path / mf.MANIFEST_PATH
    with open(path, "ab") as f:
        f.write(b'{"type":"append"}\n{"type":"item","lib":"L1","item_')
    got = _fresh_read(tmp_path)
    assert got.items["nav/id3"].size == 7 and len(got.items) == 400
    got.items.pop("nav/id0")
    mf.write(tmp_path, got)  # torn tail: rewritten in full, not appended after the torn line
    assert not any('"append"' in line for line in _lines(tmp_path))
    assert len(_fresh_read(tmp_path).items) == 399


def test_torn_base_line_still_raises(tmp_path):
    mf.write(tmp_path, _manifest(3))
    path = tmp_path / mf.MANIFEST_PATH
    path.write_bytes(path.read_bytes()[:-20])
    mf.drop_cache()
    with pytest.raises(mf.ManifestError):
        mf.read(tmp_path)


def test_large_tail_and_playlist_change_compact(tmp_path):
    m = _manifest(400)
    mf.write(tmp_path, m)
    m = mf.read(tmp_path)
    for i in range(150):  # more than APPEND_MIN and a quarter of the items
        k = f"nav/id{i}"
        m.items[k] = m.items[k].model_copy(update={"size": 1})
    mf.write(tmp_path, m)
    assert len(_lines(tmp_path)) == 402
    m = mf.read(tmp_path)
    m.items["nav/id0"] = m.items["nav/id0"].model_copy(update={"size": 2})
    mf.write(tmp_path, m)
    assert len(_lines(tmp_path)) == 404
    m.playlists.append("Інший")
    mf.write(tmp_path, m)
    assert len(_lines(tmp_path)) == 403
    got = _fresh_read(tmp_path)
    assert got.items == m.items and got.playlists == m.playlists


def test_change_made_elsewhere_is_not_appended_to(tmp_path):
    m = _manifest(400)
    mf.write(tmp_path, m)
    m = mf.read(tmp_path)
    other = mf.copy(m)
    other.items.pop("nav/id5")
    mf.forget(tmp_path)
    mf.write(tmp_path, other, compact=True)
    os.utime(tmp_path / mf.MANIFEST_PATH, ns=(1, 1))
    m.items["nav/id6"] = m.items["nav/id6"].model_copy(update={"size": 3})
    mf.write(tmp_path, m)  # cache is of another file state: full write
    assert not any('"append"' in line for line in _lines(tmp_path))


def test_a_manifest_written_by_0_2_0_reads_stays_ours_and_upgrades_in_place(tmp_path):
    """The real 0.2.0 output: the player keeps its registration (not foreign, this instance is home), its
    items and playlists, and the next write upgrades the file to the current version."""
    from pathlib import Path

    from syncmusic.core.models import DeviceConfig, DeviceIdentity
    from syncmusic.core.sync import foreign_manifest

    fixture = Path(__file__).parent.parent / "fixtures" / "upgrade" / "manifest-v0.2.0.jsonl"
    (tmp_path / mf.STATE_DIR).mkdir()
    (tmp_path / mf.MANIFEST_PATH).write_bytes(fixture.read_bytes())
    ctx = mf.Context(instance_id="inst-new", instance_name="new")
    mf.set_context(ctx)
    try:
        got = mf.read(tmp_path)
        assert got is not None
        cfg = DeviceConfig(
            id="dev-stick",
            type="usb_drive",
            name="Test stick",
            identity=DeviceIdentity(manifest_device_id="uid-test-0001"),
        )
        assert not foreign_manifest(got, cfg) and mf.is_home(got)
        keys = {f"lib-music/track{i}" for i in range(3)}
        assert set(got.items) == keys
        assert got.playlist_refs["Улюблене"] == ["lib-music/track0", "lib-music/track2"]
        mf.write(tmp_path, got)
        head = json.loads((tmp_path / mf.MANIFEST_PATH).read_text().splitlines()[0])
        assert head["manifest_version"] == mf.MANIFEST_VERSION > 1
        again = mf.read(tmp_path)
        assert set(again.items) == keys and not foreign_manifest(again, cfg)
        assert again.playlist_refs == got.playlist_refs
    finally:
        mf.set_context(mf.Context())

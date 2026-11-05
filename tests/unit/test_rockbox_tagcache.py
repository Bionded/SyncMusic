"""Rockbox tagcache reader."""

from __future__ import annotations

import datetime as dt

import pytest

from syncmusic.base.capabilities import PathRules
from syncmusic.core.models import DeviceConfig, ItemRef, Manifest, ManifestEntry, ManifestHeader
from syncmusic.devices.Rockbox import rockbox_feedback as rf
from syncmusic.devices.Rockbox.rockbox import Rockbox
from tests.contracts.harness import rockbox_tree
from tests.rockbox_tagcache import Row, write_tagcache

FAT = PathRules.for_filesystem("vfat")
NOW = dt.datetime(2026, 10, 18, 20, 0, tzinfo=dt.UTC)


def _read(rb):
    return rf.parse_tagcache(
        (rb / rf.MASTER_FILE).read_bytes(), (rb / "database_4.tcd").read_bytes()
    )


@pytest.mark.parametrize("order", ["<", ">"])
def test_synthetic_roundtrip(tmp_path, order):
    rows = [
        Row("/Music/Скрябін/Група крові.mp3", playcount=3, rating=7, lastplayed=12),
        Row("/Music/Gone.mp3", playcount=9, flag=rf.FLAG_DELETED),
        Row("/Music/b.flac", playcount=0, rating=0, length_ms=300_000),
    ]
    write_tagcache(tmp_path, rows, order)
    entries = _read(tmp_path)
    assert [(e.filename, e.playcount, e.rating, e.lastplayed) for e in entries] == [
        ("/Music/Скрябін/Група крові.mp3", 3, 7, 12),
        ("/Music/b.flac", 0, 0, 0),
    ]
    assert entries[1].length_ms == 300_000


def test_damaged_or_foreign_files_refused(tmp_path):
    write_tagcache(tmp_path, [Row("/Music/a.mp3")])
    master = (tmp_path / rf.MASTER_FILE).read_bytes()
    names = (tmp_path / "database_4.tcd").read_bytes()
    with pytest.raises(rf.TagcacheError):
        rf.parse_tagcache(b"TCH\x0f" + master[4:], names)  # older header version
    with pytest.raises(rf.TagcacheError):
        rf.parse_tagcache(master[:40], names)  # truncated
    assert rf.parse_tagcache(master, names[:16]) == []  # file name out of range: entry skipped


def test_rating_stars():
    assert [rf.rating_stars(r) for r in range(11)] == [None, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5]


def _manifest(**last) -> Manifest:
    items = {}
    for i, path in enumerate(("Music/A/a.mp3", "Music/B/b.mp3", "Music/C/c.mp3")):
        ref = ItemRef(library_id="nd", item_id=f"s{i}")
        items[ref.key()] = ManifestEntry(
            ref=ref, device_ref=path, path=path, size=1, duration_s=100, **last.get(path, {})
        )
    return Manifest(header=ManifestHeader(device_id="rb"), items=items)


def test_deltas_against_manifest():
    m = _manifest(**{"Music/A/a.mp3": {"last_playcount": 2, "last_rating": 4}})
    entries = [
        rf.TagcacheEntry("/music/a/A.MP3", 5, 8, 0, 20, 100_000),  # +3 plays, rating unchanged
        rf.TagcacheEntry("/Music/B/b.mp3", 1, 3, 0, 30, 100_000),  # +1 play (latest), new rating
        rf.TagcacheEntry("/Music/C/c.mp3", 0, 0, 0, 0, 100_000),
        rf.TagcacheEntry("/Music/Other.mp3", 4, 0, 0, 25, 100_000),
    ]
    fb = rf.tagcache_feedback(entries, m, FAT, now=NOW, device_id="rb")
    assert fb.unmapped == 1
    assert fb.play_counts == {"nd/s0": 5, "nd/s1": 1, "nd/s2": 0}
    assert fb.ratings == {"nd/s1": 2}
    assert [p.ref.item_id for p in fb.plays] == ["s0", "s0", "s0", "s1"]
    assert fb.plays[-1].played_at == NOW  # highest lastplayed = most recent
    assert fb.plays[0].played_at == NOW - dt.timedelta(seconds=300)
    assert all(p.played_at_estimated for p in fb.plays)
    # rating removed on the device; no new plays when the count did not grow
    m2 = _manifest(**{"Music/A/a.mp3": {"last_playcount": 5, "last_rating": 4}})
    unrated = rf.TagcacheEntry("/Music/A/a.mp3", 5, 0, 0, 20, 100_000)
    fb2 = rf.tagcache_feedback([unrated], m2, FAT, now=NOW, device_id="rb")
    assert fb2.plays == [] and fb2.ratings == {"nd/s0": None}


async def test_device_reads_tagcache(tmp_path):
    mount = rockbox_tree(tmp_path / "rb")
    dev = Rockbox(DeviceConfig(id="rb", type="rockbox", name="RB"), mount, None)
    assert dev.read_tagcache() is None
    write_tagcache(mount / ".rockbox", [Row("/Music/a.mp3", playcount=2)])
    assert [e.playcount for e in dev.read_tagcache() or []] == [2]
    (mount / ".rockbox" / rf.MASTER_FILE).write_bytes(b"junk")
    assert dev.read_tagcache() is None

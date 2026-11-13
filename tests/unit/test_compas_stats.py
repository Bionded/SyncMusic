# SPDX-License-Identifier: GPL-3.0-or-later
"""Library hearts and play counts patched into a Compás tagcache in place."""

import json
import struct

import pytest

from syncmusic.base.device import DeviceSession
from syncmusic.devices.HibyR1 import compas_feedback as cf
from syncmusic.devices.HibyR1.compas_stats import Patched, StatsUpdate, patch_stats
from tests.unit.test_compas_feedback import write_tagcache

ROWS = [
    ("Music/Ours/01.flac", 2, 0, 100),
    ("Music/Theirs/02.mp3", 7, 1, 900),  # the user's own file: never touched
    ("Music/Ours/03.mp3", 9, 1, 500),
]


def _master(tmp_path):
    return cf.master_path(tmp_path).read_bytes()


def test_raises_counts_sets_hearts_and_keeps_other_rows(tmp_path):
    write_tagcache(tmp_path, ROWS)
    before = _master(tmp_path)
    got = patch_stats(
        tmp_path,
        {
            "Music/Ours/01.flac": StatsUpdate(playcount=5, last_played=200, favourite=True),
            "Music/Ours/03.mp3": StatsUpdate(playcount=4, last_played=50, favourite=False),
            "Music/Ours/new.mp3": StatsUpdate(playcount=1),  # not indexed yet
        },
    )
    assert got == {
        "Music/Ours/01.flac": Patched(5, 1, 200),
        "Music/Ours/03.mp3": Patched(9, 0, 500),  # never lower, never earlier; heart taken away
    }
    after = _master(tmp_path)
    assert len(after) == len(before)
    row = cf.ENTRY_SIZE
    assert after[: cf.MASTER_HEADER + row] != before[: cf.MASTER_HEADER + row]
    assert (
        after[cf.MASTER_HEADER + row : cf.MASTER_HEADER + 2 * row]
        == (before[cf.MASTER_HEADER + row : cf.MASTER_HEADER + 2 * row])
    )
    data = cf.read_card(tmp_path)
    assert data.favourites == {"Music/Ours/01.flac", "Music/Theirs/02.mp3"}
    assert data.play_counts["Music/Ours/01.flac"] == (5, 200)


def test_second_patch_writes_nothing(tmp_path):
    write_tagcache(tmp_path, ROWS)
    u = {"Music/Ours/01.flac": StatsUpdate(playcount=5, favourite=True)}
    patch_stats(tmp_path, u)
    path = cf.master_path(tmp_path)
    mtime = path.stat().st_mtime_ns
    patch_stats(tmp_path, u)
    assert path.stat().st_mtime_ns == mtime


def test_refused_while_written_or_missing(tmp_path):
    with pytest.raises(cf.CompasDbError):
        patch_stats(tmp_path, {})
    write_tagcache(tmp_path, ROWS)
    path = cf.master_path(tmp_path)
    raw = bytearray(path.read_bytes())
    struct.pack_into("<i", raw, 20, 1)  # dirty
    path.write_bytes(bytes(raw))
    with pytest.raises(cf.CompasDbError):
        patch_stats(tmp_path, {"Music/Ours/01.flac": StatsUpdate(playcount=1)})


def _session(data):

    s = DeviceSession()
    s.data.update(data)
    return s


def _setup(tmp_path, fmt=8):
    import json

    from syncmusic.core.models import (
        DeviceConfig,
        ItemRef,
        Manifest,
        ManifestEntry,
        ManifestHeader,
        Track,
    )
    from syncmusic.devices.HibyR1.compas import HibyCompas

    write_tagcache(tmp_path, ROWS)
    (tmp_path / ".compas" / "device.json").write_text(json.dumps({"database_format": fmt}))
    dev = HibyCompas(DeviceConfig(id="c", type="hiby_compas", name="C"), tmp_path, None)
    refs = {
        p: ItemRef(library_id="lib", item_id=str(i))
        for i, p in enumerate(["Music/Ours/01.flac", "Music/Ours/03.mp3", "Music/Ours/new.mp3"])
    }
    m = Manifest(
        header=ManifestHeader(device_id="c"),
        items={
            r.key(): ManifestEntry(ref=r, device_ref=p, path=p, size=1, last_playcount=2)
            for p, r in refs.items()
        },
    )
    m.items[refs["Music/Ours/03.mp3"].key()].last_starred = True
    tracks = {r.key(): Track(ref=r, title="t", play_count=6) for r in refs.values()}
    starred = {
        refs["Music/Ours/01.flac"].key(): True,
        refs["Music/Ours/03.mp3"].key(): False,
        refs["Music/Ours/new.mp3"].key(): True,
    }
    return dev, m, refs, tracks, starred


@pytest.mark.parametrize("pushed", [True, False])
def test_commit_patches_and_moves_the_baselines(tmp_path, pushed):
    import asyncio

    dev, m, refs, tracks, starred = _setup(tmp_path)
    (tmp_path / ".syncmusic").mkdir(exist_ok=True)
    dev._state_file().write_text('["Music/Ours/03.mp3"]')
    s = _session({"library_tracks": tracks, "library_starred": starred, "reverse_ok": pushed})
    asyncio.run(dev._patch_stats(s, m))
    data = cf.read_card(tmp_path)
    # the heart taken away in the library goes (the reverse sync had seen it); the new one is added
    assert data.favourites == {"Music/Ours/01.flac", "Music/Theirs/02.mp3"}
    e1, e3, new = (m.items[refs[p].key()] for p in refs)
    assert (e1.last_starred, e3.last_starred, new.last_starred) == (True, False, None)
    assert data.play_counts["Music/Ours/01.flac"][0] == (6 if pushed else 2)
    assert e1.last_playcount == (6 if pushed else 2)  # the next reverse sync sees no new plays
    assert e3.last_playcount == (9 if pushed else 2)
    assert set(json.loads(dev._state_file().read_text())) == {"Music/Ours/01.flac"}


def test_other_database_format_is_left_alone(tmp_path):
    import asyncio

    dev, m, _refs, tracks, starred = _setup(tmp_path, fmt=9)
    before = _master(tmp_path)
    asyncio.run(
        dev._patch_stats(_session({"library_tracks": tracks, "library_starred": starred}), m)
    )
    assert _master(tmp_path) == before

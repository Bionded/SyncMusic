"""Rockbox read_feedback source priority and log rotation."""

from __future__ import annotations

import datetime as dt

from syncmusic.base.device import DeviceSession
from syncmusic.core.models import DeviceConfig, ItemRef, Manifest, ManifestEntry, ManifestHeader
from syncmusic.devices.Rockbox.rockbox import Rockbox
from tests.contracts.harness import rockbox_tree
from tests.media import make_music
from tests.rockbox_tagcache import Row, write_tagcache

SONG = "Music/Скрябін/0101 - Група крові.mp3"
SCROBBLER = (
    "#AUDIOSCROBBLER/1.1\n#TZ/UNKNOWN\n#CLIENT/Rockbox ipodmini2g 4.0\n"
    "Скрябін\tГрупа крові\tГрупа крові\t1\t285\tL\t0\t\n"
)


def _setup(tmp_path, **settings):
    mount = rockbox_tree(tmp_path / "rb")
    make_music(mount / SONG, title="Група крові", artist="Скрябін", album="Група крові")
    ref = ItemRef(library_id="nd", item_id="s0")
    entry = ManifestEntry(
        ref=ref, device_ref=SONG, path=SONG, size=1, duration_s=285, last_playcount=1
    )
    m = Manifest(header=ManifestHeader(device_id="rb"), items={ref.key(): entry})
    dev = Rockbox(DeviceConfig(id="rb", type="rockbox", name="RB", settings=settings), mount, None)
    return dev, m


def _playback(dev, n):
    lines = "".join(f"{1000 * i}:285000:285000:/{SONG}\n" for i in range(n))
    (dev.mount / ".rockbox" / "playback.log").write_text("# Started Ver. 4.0\n" + lines)


async def test_source_priority(tmp_path):
    dev, m = _setup(tmp_path)
    write_tagcache(dev.mount / ".rockbox", [Row(f"/{SONG}", playcount=5, rating=6, lastplayed=3)])
    fb = await dev.read_feedback(m)  # tagcache only: 5 - 1 = 4 plays
    assert len(fb.plays) == 4 and fb.ratings == {"nd/s0": 3} and fb.play_counts == {"nd/s0": 5}
    (dev.mount / ".scrobbler-timeless.log").write_text(SCROBBLER)
    fb = await dev.read_feedback(m)  # scrobbler log wins for plays, tagcache for ratings
    assert len(fb.plays) == 1 and fb.ratings == {"nd/s0": 3} and fb.play_counts == {"nd/s0": 5}
    _playback(dev, 2)
    fb = await dev.read_feedback(m)  # playback.log wins
    assert len(fb.plays) == 2 and fb.play_counts == {"nd/s0": 5}
    assert all(p.played_at_estimated for p in fb.plays)  # ms since boot


async def test_rotation_only_of_read_logs_after_commit(tmp_path):
    dev, m = _setup(tmp_path, keep_rotated=2)
    _playback(dev, 1)
    (dev.mount / ".scrobbler.log").write_text(SCROBBLER)
    (dev.mount / ".rockbox" / "playback_0001.log").write_text(f"5:285000:285000:/{SONG}\n")
    s = DeviceSession(m)
    fb = await dev.read_feedback(m)
    assert len(fb.plays) == 2
    # the reverse sync failed: nothing rotated, the next read sees the same plays
    dev2, _ = _setup(tmp_path)
    assert len((await dev2.read_feedback(m)).plays) == 2
    await dev.reset_feedback(s, fb)
    rb = dev.mount / ".rockbox"
    assert not (rb / "playback.log").exists() and not (rb / "playback_0001.log").exists()
    assert not (dev.mount / ".scrobbler.log").exists()  # both kinds: never counted again
    rotated = sorted(p.name for p in [*rb.glob("*.syncmusic-*"), *dev.mount.glob(".*.syncmusic-*")])
    assert len(rotated) == 3 and rotated[0].startswith(".scrobbler.log.syncmusic-")
    assert (await dev.read_feedback(m)).plays == []
    await dev.reset_feedback(s, fb)  # nothing read: nothing to rotate, no error


async def test_changed_log_not_rotated_and_old_rotations_deleted(tmp_path, monkeypatch):
    dev, m = _setup(tmp_path, keep_rotated=2)
    rb = dev.mount / ".rockbox"
    s = DeviceSession(m)
    times = iter(dt.datetime(2026, 10, d, tzinfo=dt.UTC) for d in range(1, 10))

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return next(times)

    import syncmusic.devices.Rockbox.rockbox as rockbox

    monkeypatch.setattr(rockbox.dt, "datetime", Clock)
    for _ in range(3):
        _playback(dev, 1)
        fb = await dev.read_feedback(m)
        await dev.reset_feedback(s, fb)
    names = sorted(p.name for p in rb.glob("playback.log.syncmusic-*"))
    assert len(names) == 2  # the oldest rotation is gone
    _playback(dev, 1)
    fb = await dev.read_feedback(m)
    _playback(
        dev, 3
    )  # the player wrote more after the read (cannot happen in USB mode, but be safe)
    await dev.reset_feedback(s, fb)
    assert (rb / "playback.log").exists()

"""Rockbox playback.log reader."""

from __future__ import annotations

import datetime as dt

from syncmusic.base.capabilities import PathRules
from syncmusic.core.models import DeviceConfig, ItemRef, Manifest, ManifestEntry, ManifestHeader
from syncmusic.devices.Rockbox import rockbox_feedback as rf
from syncmusic.devices.Rockbox.rockbox import Rockbox
from tests.contracts.harness import rockbox_tree

FAT = PathRules.for_filesystem("vfat")
NOW = dt.datetime(2026, 10, 18, 20, 0, tzinfo=dt.UTC)
SONG = "Music/Скрябін/1988 - Група крові/0101 - Група крові.mp3"


def _manifest(*paths: str) -> Manifest:
    items = {}
    for i, p in enumerate(paths):
        ref = ItemRef(library_id="nd", item_id=f"s{i}")
        items[ref.key()] = ManifestEntry(ref=ref, device_ref=p, path=p, size=1)
    return Manifest(header=ManifestHeader(device_id="rb"), items=items)


def _wall(d: dt.datetime) -> int:
    """Rockbox's stamp for a local wall-clock time."""
    return int((d.replace(tzinfo=None) - dt.datetime(1970, 1, 1)).total_seconds())


def test_parse_skips_comments_and_malformed():
    text = (
        "# Started Ver. 4.0 20261018 Time 193000\n"
        f"1760815800:200000:240000:/{SONG}\n"
        "garbage\n"
        "1:2:x:/Music/a.mp3\n"
        "\n"
        "1760815900:1000:2000:/Music/with:colon.mp3\n"
        "5:6:7:relative.mp3\n"
    )
    entries = rf.parse_playback_log(text)
    assert [e.path for e in entries] == [f"/{SONG}", "/Music/with:colon.mp3"]
    assert entries[0] == rf.PlaybackEntry(1760815800, 200000, 240000, f"/{SONG}")


def test_scrobble_rule():
    assert rf.counts_as_play(120_000, 240_000, 0.5, 240)  # half
    assert not rf.counts_as_play(119_999, 240_000, 0.5, 240)
    assert rf.counts_as_play(240_000, 3_600_000, 0.5, 240)  # 4 minutes of a long track
    assert not rf.counts_as_play(10_000, 0, 0.5, 240)  # unknown length: only the seconds rule


def test_plays_mapped_case_insensitive_with_rtc():
    m = _manifest(SONG, "Music/Other.mp3")
    played = dt.datetime(2026, 10, 18, 19, 30)  # local wall clock
    entries = [
        rf.PlaybackEntry(_wall(played), 200_000, 240_000, "/" + SONG.upper()),
        rf.PlaybackEntry(_wall(played) + 300, 5_000, 240_000, "/Music/Other.mp3"),  # skipped
        rf.PlaybackEntry(_wall(played) + 600, 300_000, 300_000, "/<HDD0>/Music/Unknown.mp3"),
    ]
    plays, unmapped = rf.playback_plays(entries, m, FAT, has_rtc=True, now=NOW, device_id="rb")
    assert unmapped == 1
    assert len(plays) == 1 and plays[0].ref.item_id == "s0"
    assert plays[0].played_at == played.astimezone().astimezone(dt.UTC)
    assert not plays[0].played_at_estimated and plays[0].source_device == "rb"
    # case-sensitive file system: no match
    ext4 = PathRules.for_filesystem("ext4")
    plays, unmapped = rf.playback_plays(entries, m, ext4, has_rtc=True, now=NOW, device_id="rb")
    assert plays == [] and unmapped == 2


def test_no_rtc_or_unset_clock_estimated_backwards_from_now():
    m = _manifest(SONG)
    entries = [
        rf.PlaybackEntry(123_456, 200_000, 240_000, f"/{SONG}"),  # ms since boot
        rf.PlaybackEntry(323_456, 180_000, 240_000, f"/{SONG}"),
    ]
    plays, _ = rf.playback_plays(entries, m, FAT, has_rtc=False, now=NOW, device_id="rb")
    assert [p.played_at for p in plays] == [NOW - dt.timedelta(seconds=180), NOW]
    assert all(p.played_at_estimated for p in plays)
    # RTC present but never set (the mini boots at 2000-01-01): implausible → estimated too
    unset = _wall(dt.datetime(2000, 1, 1, 0, 5))
    entries = [rf.PlaybackEntry(unset, 200_000, 240_000, f"/{SONG}")]
    plays, _ = rf.playback_plays(entries, m, FAT, has_rtc=True, now=NOW, device_id="rb")
    assert plays[0].played_at == NOW and plays[0].played_at_estimated


async def test_read_feedback_reads_rotated_and_current_log(tmp_path):
    mount = rockbox_tree(tmp_path / "rb")
    dev = Rockbox(DeviceConfig(id="rb", type="rockbox", name="RB"), mount, None)
    m = _manifest(SONG)
    stamp = _wall(dt.datetime(2026, 10, 1, 12, 0))
    line = f"{stamp}:200000:240000:/{SONG}\n"
    (mount / ".rockbox" / "playback_0001.log").write_text("# Started Ver. 4.0\n" + line)
    (mount / ".rockbox" / "playback.log").write_text(line.replace(str(stamp), str(stamp + 600)))
    (mount / ".rockbox" / "playback_x.log").write_text(line)  # not Rockbox's name
    assert [p.name for p in dev.playback_logs()] == ["playback_0001.log", "playback.log"]
    fb = await dev.read_feedback(m)
    assert len(fb.plays) == 2 and fb.plays[0].played_at < fb.plays[1].played_at
    assert (await dev.read_feedback(None)).is_empty()
    dev2 = Rockbox(
        DeviceConfig(id="rb", type="rockbox", name="RB", settings={"play_min_seconds": 100}),
        mount,
        None,
    )
    (mount / ".rockbox" / "playback.log").write_text(f"{stamp}:150000:400000:/{SONG}\n")
    assert len((await dev2.read_feedback(m)).plays) == 2
    assert len((await dev.read_feedback(m)).plays) == 1


async def test_old_reverse_sync_thresholds_move_to_the_rockbox_setting(tmp_path):
    """one pair of play thresholds (the Rockbox settings); a value an older build stored in
    reverse_sync.min_play_* moves there and changes which log entries count as plays."""
    old = {
        "id": "rb",
        "type": "rockbox",
        "name": "RB",
        "reverse_sync": {"plays": True, "min_play_fraction": 0.5, "min_play_seconds": 100},
    }
    cfg = DeviceConfig.model_validate(old)
    assert cfg.settings == {"play_min_seconds": 100}
    assert "min_play_seconds" not in cfg.reverse_sync.model_dump()
    # an explicit Rockbox value wins; other device types just drop the old pair
    both = {**old, "settings": {"play_min_seconds": 300}}
    assert DeviceConfig.model_validate(both).settings == {"play_min_seconds": 300}
    flash = DeviceConfig.model_validate({**old, "type": "flashdrive"})
    assert flash.settings == {} and "min_play_fraction" not in flash.model_dump()["reverse_sync"]

    mount = rockbox_tree(tmp_path / "rb")
    m = _manifest(SONG)
    stamp = _wall(dt.datetime(2026, 10, 1, 12, 0))
    (mount / ".rockbox" / "playback.log").write_text(f"{stamp}:150000:400000:/{SONG}\n")
    assert len((await Rockbox(cfg, mount, None).read_feedback(m)).plays) == 1  # 150 s >= 100 s
    default = DeviceConfig(id="rb", type="rockbox", name="RB")
    assert len((await Rockbox(default, mount, None).read_feedback(m)).plays) == 0

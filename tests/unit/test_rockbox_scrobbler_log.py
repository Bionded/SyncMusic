"""Rockbox .scrobbler.log reader."""

from __future__ import annotations

import datetime as dt

from syncmusic.core.models import DeviceConfig, ItemRef, Manifest, ManifestEntry, ManifestHeader
from syncmusic.devices.Rockbox import rockbox_feedback as rf
from syncmusic.devices.Rockbox.rockbox import Rockbox
from tests.contracts.harness import rockbox_tree
from tests.media import make_music

NOW = dt.datetime(2026, 10, 18, 20, 0, tzinfo=dt.UTC)
HEADER = (
    "#AUDIOSCROBBLER/1.1\n#TZ/{tz}\n#CLIENT/Rockbox ipodmini2g 4.0\n"
    "#ARTIST\t#ALBUM\t#TITLE\t#TRACKNUM\t#LENGTH\t#RATING\t#TIMESTAMP\t#MUSICBRAINZ_TRACKID\n"
)


def _entry(path: str, i: int) -> tuple[str, ManifestEntry]:
    ref = ItemRef(library_id="nd", item_id=f"s{i}")
    return ref.key(), ManifestEntry(ref=ref, device_ref=path, path=path, size=1)


def _index(*tracks: tuple[str, str, str, str]) -> tuple[rf.TagIndex, dict[str, ManifestEntry]]:
    entries, tags = {}, {}
    for i, (artist, album, title, path) in enumerate(tracks):
        key, e = _entry(path, i)
        entries[key] = e
        tags[key] = rf.TrackTags(artist, album, title, path.rsplit("/", 1)[-1])
    return rf.TagIndex(tags, entries), entries


def test_parse_header_entries_and_malformed():
    text = HEADER.format(tz="UTC") + (
        "Скрябін\tГрупа крові\tГрупа крові\t1\t285\tL\t1760814000\t\n"
        "Тартак\t\tОрбіт без цукру\t-1\t250\tS\t1760814300\n"
        "bad line\n"
        "A\tB\tC\t1\tx\tL\t1\t\n"
        "A\tB\tC\t1\t10\tQ\t1\t\n"
    )
    tz, entries = rf.parse_scrobbler_log(text)
    assert tz == "UTC"
    assert entries == [
        rf.ScrobblerEntry("Скрябін", "Група крові", "Група крові", 285, True, 1760814000),
        rf.ScrobblerEntry("Тартак", "", "Орбіт без цукру", 250, False, 1760814300),
    ]


def test_only_listened_count_and_mapping_by_tags():
    index, _ = _index(
        ("Скрябін", "Група крові", "Група крові", "Music/Скрябін/a.mp3"),
        ("Тартак", "Гранатовий альбом", "Орбіт без цукру", "Music/Тартак/b.mp3"),
        ("Скрябін", "Ніч", "Зозуля", "Music/Скрябін/c.mp3"),
        ("Скрябін", "Зірка", "Зозуля", "Music/Скрябін/d.mp3"),
    )
    entries = [
        rf.ScrobblerEntry("СКРЯБІН", "група  крові", "Група Крові", 285, True, 1760814000),
        rf.ScrobblerEntry("Тартак", "", "Орбіт без цукру", 250, True, 1760814300),  # no album
        rf.ScrobblerEntry("Тартак", "", "Орбіт без цукру", 250, False, 1760814600),  # skipped
        rf.ScrobblerEntry("Скрябін", "", "Зозуля", 400, True, 1760814900),  # ambiguous
        rf.ScrobblerEntry("Скрябін", "Зірка", "Зозуля", 400, True, 1760815200),  # album decides
        rf.ScrobblerEntry("Nobody", "", "Nothing", 100, True, 1760815500),
    ]
    plays, unmapped = rf.scrobbler_plays(entries, "UTC", index, now=NOW, device_id="rb")
    assert [p.ref.item_id for p in plays] == ["s0", "s1", "s3"]
    assert unmapped == 2
    assert plays[0].played_at == dt.datetime.fromtimestamp(1760814000, dt.UTC)
    assert not any(p.played_at_estimated for p in plays)


def test_tz_unknown_is_local_wall_time():
    index, _ = _index(("A", "B", "C", "Music/c.mp3"))
    stamp = 1760814000
    entries = [rf.ScrobblerEntry("A", "B", "C", 100, True, stamp)]
    plays, _ = rf.scrobbler_plays(entries, "UNKNOWN", index, now=NOW, device_id="rb")
    assert plays[0].played_at == rf.local_wall_to_utc(stamp)


def test_timeless_and_untagged():
    index, _ = _index(
        ("A", "B", "C", "Music/A/0101 - C.mp3"), ("", "", "", "Music/x/Без тегів.mp3")
    )
    entries = [
        rf.ScrobblerEntry("A", "B", "C", 100, True, 0),
        rf.ScrobblerEntry(rf.UNTAGGED, "", "Без тегів.mp3", 60, True, 0),
    ]
    plays, unmapped = rf.scrobbler_plays(entries, "UNKNOWN", index, now=NOW, device_id="rb")
    assert unmapped == 0 and [p.ref.item_id for p in plays] == ["s0", "s1"]
    assert [p.played_at for p in plays] == [NOW - dt.timedelta(seconds=60), NOW]
    assert all(p.played_at_estimated for p in plays)


async def test_read_feedback_from_scrobbler_logs_with_device_tags(tmp_path):
    mount = rockbox_tree(tmp_path / "rb")
    rel = "Music/Скрябін/1988 - Група крові/0101 - Група крові.mp3"
    make_music(mount / rel, title="Група крові", artist="Скрябін", album="Група крові")
    key, entry = _entry(rel, 0)
    m = Manifest(header=ManifestHeader(device_id="rb"), items={key: entry})
    dev = Rockbox(DeviceConfig(id="rb", type="rockbox", name="RB"), mount, None)
    assert (await dev.read_feedback(m)).is_empty()
    (mount / ".scrobbler.log").write_text(
        HEADER.format(tz="UNKNOWN") + "Скрябін\tГрупа крові\tГрупа крові\t1\t285\tL\t1760814000\t\n"
    )
    (mount / ".scrobbler-timeless.log").write_text(
        HEADER.format(tz="UNKNOWN")
        + "Скрябін\t\tГрупа крові\t1\t285\tL\t0\t\nX\t\tY\t1\t9\tL\t0\t\n"
    )
    fb = await dev.read_feedback(m)
    assert len(fb.plays) == 2 and fb.unmapped == 1
    assert [p.played_at_estimated for p in fb.plays] == [False, True]
    # playback.log takes priority over the scrobbler logs once it has entries
    (mount / ".rockbox" / "playback.log").write_text("# Started Ver. 4.0\n")
    assert len((await dev.read_feedback(m)).plays) == 2
    with (mount / ".rockbox" / "playback.log").open("a") as f:
        f.write(f"1760814000:2000:285000:/{rel}\n")  # a skip: no plays, but the source wins
    assert (await dev.read_feedback(m)).is_empty()

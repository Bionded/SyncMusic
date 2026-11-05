"""Device path layout and sanitisation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from syncmusic.base.capabilities import PathRules
from syncmusic.core.models import MediaKind
from syncmusic.devices import paths
from syncmusic.devices.paths import (
    PathLayout,
    name_len,
    path_key,
    playlist_path,
    render,
    sanitize_component,
    unique,
)
from tests.factories import track

FS = ["vfat", "exfat", "ntfs", "ext4"]
VFAT = PathRules.for_filesystem("vfat")
EXT4 = PathRules.for_filesystem("ext4")


@pytest.mark.parametrize("fs", FS)
def test_forbidden_and_control_chars(fs):
    rules = PathRules.for_filesystem(fs)
    out = sanitize_component('AC/DC: "Back" <in> Black?*|\\\x01', rules)
    assert "/" not in out and "\x01" not in out
    if fs != "ext4":
        assert not set('<>:"\\|?*') & set(out)
    else:
        assert ': "' in out and "?" in out  # ext4 allows everything but '/' and NUL


@pytest.mark.parametrize("fs", FS)
def test_trailing_dots_spaces(fs):
    rules = PathRules.for_filesystem(fs)
    out = sanitize_component("Help... ", rules)
    assert out == ("Help" if fs != "ext4" else "Help...")
    assert sanitize_component("Song.  .mp3", rules, has_ext=True).endswith(".mp3")
    assert sanitize_component("...", rules) in ("_", "_..") and sanitize_component("", rules) == "_"
    assert sanitize_component(".hidden", rules) == "_hidden"


@pytest.mark.parametrize("fs", FS)
def test_name_length_with_cyrillic_and_emoji(fs):
    rules = PathRules.for_filesystem(fs)
    name = "Ж" * 200 + "🎵" * 100 + ".flac"
    out = sanitize_component(name, rules, has_ext=True)
    assert out.endswith(".flac")
    assert name_len(out, rules) <= 255
    if fs == "ext4":
        assert len(out.encode()) <= 255  # bytes: 2 per Cyrillic letter
    else:
        assert len(out.encode("utf-16-le")) // 2 <= 255  # emoji = 2 UTF-16 units
        assert out.startswith("Ж" * 200)
    out.encode("utf-8")  # no split surrogates


@pytest.mark.parametrize("fs", ["vfat", "exfat", "ntfs"])
@pytest.mark.parametrize("name", ["CON", "nul", "Com1", "LPT9.txt", "aux.mp3"])
def test_reserved_dos_names(fs, name):
    rules = PathRules.for_filesystem(fs)
    out = sanitize_component(name, rules, has_ext="." in name)
    assert out.split(".")[0].upper() not in {"CON", "NUL", "COM1", "LPT9", "AUX"}


def test_reserved_names_fine_on_ext4():
    assert sanitize_component("CON", EXT4) == "CON"


def test_ascii_only_transliterates():
    rules = VFAT.model_copy(update={"ascii_only": True})
    assert sanitize_component("Тартак - Орбіт", rules) == "Tartak - Orbit"
    assert sanitize_component("Beyoncé Ølsen", rules) == "Beyonce _lsen"
    assert sanitize_component("Щука", rules) == "Shchuka"


def test_music_template_full():
    t = track(
        "1",
        title="Тут не Франція",
        artist="Калуш",
        album="Тяжкі тілесні",
        year=2011,
        track_no=2,
        disc_no=1,
    )
    assert render(t, "mp3", VFAT) == "Music/Калуш/2011 - Тяжкі тілесні/0102 - Тут не Франція.mp3"


def test_music_template_missing_fields():
    t = track("1", title="Song", artist=None, album=None, year=None, track_no=None, disc_no=None)
    assert render(t, "mp3", VFAT) == "Music/Unknown Artist/Unknown Album/Song.mp3"
    t = track("1", title="Song", artist="A", album="B", track_no=3)
    assert render(t, ".flac", VFAT) == "Music/A/B/03 - Song.flac"


def test_album_artist_preferred():
    t = track(
        "1", title="S", artist="Guest", album_artist="Various Artists", album="Hits", year=1999
    )
    assert render(t, "mp3", VFAT).startswith("Music/Various Artists/1999 - Hits/")


def test_podcast_template():
    t = track(
        "1",
        kind=MediaKind.PODCAST,
        title="Ep 1: Start?",
        series="Warhammer",
        published=datetime(2024, 5, 6, tzinfo=UTC),
    )
    assert render(t, "m4a", VFAT) == "Podcasts/Warhammer/2024-05-06 - Ep 1_ Start_.m4a"
    t = track("2", kind=MediaKind.PODCAST, title="Ep", series=None, album=None)
    assert render(t, "m4a", VFAT) == "Podcasts/Unknown Podcast/Ep.m4a"


def test_audiobook_template():
    t = track(
        "1",
        kind=MediaKind.AUDIOBOOK,
        title="Розділ 1",
        author="Кінг",
        series="Воно",
        chapter_index=1,
    )
    assert render(t, "mp3", VFAT) == "Audiobooks/Кінг - Воно/001 - Розділ 1.mp3"
    t = track(
        "1",
        kind=MediaKind.AUDIOBOOK,
        title="Ch",
        artist="Author",
        series=None,
        chapter_index=None,
        track_no=7,
    )
    assert render(t, "mp3", VFAT) == "Audiobooks/Author/007 - Ch.mp3"


def test_custom_layout_and_validation():
    layout = PathLayout(music="{artist}/{album}/{track:02} {title}")
    t = track("1", title="T", artist="A", album="B", track_no=5)
    assert render(t, "ogg", VFAT, layout) == "A/B/05 T.ogg"
    for bad in ["Music/{nope}", "{title!r}", "{title:02}", "{track:>5}", "/"]:
        with pytest.raises(ValueError):
            PathLayout(music=bad)


@pytest.mark.parametrize("fs", FS)
def test_path_length_cap_keeps_extension(fs):
    rules = PathRules.for_filesystem(fs)
    t = track("1", title="T" * 200, artist="A" * 200, album="B" * 200, year=2000, track_no=1)
    rel = render(t, "flac", rules)
    assert len(rel) <= rules.max_path_len
    assert rel.startswith("Music/") and rel.endswith(".flac")
    assert all(name_len(p, rules) <= rules.max_name_len for p in rel.split("/"))


@pytest.mark.parametrize("fs", FS)
def test_case_insensitive_collisions(fs):
    rules = PathRules.for_filesystem(fs)
    taken: set[str] = set()
    out = []
    for rel in [
        "Music/ABBA/Song.mp3",
        "Music/abba/song.mp3",
        "Music/ABBA/Song.mp3",
        "Music/ABBA/Song.mp3",
    ]:
        got = unique(rel, taken, rules)
        taken.add(path_key(got, rules))
        out.append(got)
    assert len({path_key(p, rules) for p in out}) == 4
    if fs == "ext4":
        assert out[1] == "Music/abba/song.mp3"
        assert out[2] == "Music/ABBA/Song (2).mp3"
    else:
        assert out[1] == "Music/abba/song (2).mp3"
        assert out[2] == "Music/ABBA/Song (3).mp3"


def test_unique_suffix_respects_name_length():
    rel = "Music/A/" + "x" * 251 + ".mp3"
    taken = {path_key(rel, VFAT)}
    got = unique(rel, taken, VFAT)
    assert got.endswith(" (2).mp3") and len(got.split("/")[-1]) <= 255


def test_playlist_path():
    assert playlist_path("Мій: улюблене", VFAT) == "Playlists/Мій_ улюблене.m3u8"


def test_video_without_series_goes_straight_into_videos():
    """a movie (no series, no album) had an empty folder rendered as 'Videos/_/'."""
    movie = track("m", title="Довгий фільм", kind=MediaKind.VIDEO, album=None)
    assert render(movie, "mkv", EXT4) == "Videos/Довгий фільм.mkv"
    ep = track("e", title="S01E01", kind=MediaKind.VIDEO, series="Мультики")
    assert render(ep, "m4v", EXT4) == "Videos/Мультики/S01E01.m4v"


def test_video_episode_path_carries_season_and_episode():
    """same-titled episodes of two seasons got ' (2)'; 'Episode 10' sorted before 'Episode 2'."""
    e1 = track(
        "a",
        title="Пілот",
        kind=MediaKind.VIDEO,
        series="Мультики",
        extra={"season": 1, "episode": 1},
    )
    e2 = track(
        "b",
        title="Пілот",
        kind=MediaKind.VIDEO,
        series="Мультики",
        extra={"season": 2, "episode": 1},
    )
    assert render(e1, "mp4", VFAT) == "Videos/Мультики/S01E01 - Пілот.mp4"
    assert render(e2, "mp4", VFAT) == "Videos/Мультики/S02E01 - Пілот.mp4"
    no_season = track("c", title="X", kind=MediaKind.VIDEO, series="S", extra={"episode": 10})
    assert render(no_season, "mp4", VFAT) == "Videos/S/E10 - X.mp4"
    coded = track(
        "d", title="S01E03", kind=MediaKind.VIDEO, series="S", extra={"season": 1, "episode": 3}
    )
    assert render(coded, "mp4", VFAT) == "Videos/S/S01E03.mp4"
    movie = track("m", title="Фільм", kind=MediaKind.VIDEO, album=None)
    assert render(movie, "mkv", VFAT) == "Videos/Фільм.mkv"


def test_multi_disc_template():
    """2+ discs → per-disc folder; a single disc (or unknown total) stays flat; {disc_total}."""
    one = track("a", album_artist="A", album="X", year=2001, disc_no=1, track_no=3, title="T")
    assert render(one, "mp3", EXT4) == "Music/A/2001 - X/0103 - T.mp3"
    assert (
        render(one.model_copy(update={"disc_total": 1}), "mp3", EXT4)
        == "Music/A/2001 - X/0103 - T.mp3"
    )
    two = one.model_copy(update={"disc_no": 2, "disc_total": 2})
    assert render(two, "mp3", EXT4) == "Music/A/2001 - X/CD02/03 - T.mp3"
    custom = PathLayout(
        music_multi_disc="Music/{album} (disc {disc} of {disc_total})/{track:02} {title}"
    )
    assert render(two, "mp3", EXT4, custom) == "Music/X (disc 2 of 2)/03 T.mp3"
    assert render(two.model_copy(update={"kind": MediaKind.AUDIOBOOK}), "mp3", EXT4).startswith(
        "Audiobooks/"
    )
    with pytest.raises(ValueError):
        PathLayout(music_multi_disc="{nope}")


def test_spill_into_numbered_folders():
    """a folder with `max_files_per_folder` entries spills into "<folder> (2)", "(3)" ..."""
    rules = VFAT.model_copy(update={"max_files_per_folder": 3})
    rel = "Music/Skryabin/Album/01 Song.mp3"
    assert paths.spill(rel, {}, 1, rules) == rel
    assert paths.spill(rel, {"music/skryabin/album": 2}, 1, rules) == rel
    assert (
        paths.spill(rel, {"music/skryabin/album": 2}, 2, rules)
        == "Music/Skryabin/Album (2)/01 Song.mp3"
    )
    full = {"music/skryabin/album": 3, "music/skryabin/album (2)": 3}
    assert paths.spill(rel, full, 1, rules) == "Music/Skryabin/Album (3)/01 Song.mp3"
    assert paths.spill("Song.mp3", {".": 9}, 1, rules) == "Song.mp3"  # the top level never spills
    assert paths.spill(rel, full, 1, VFAT) == rel  # no limit
    short = rules.model_copy(update={"max_name_len": 8})
    assert paths.spill("M/Abcdefgh/x.mp3", {"m/abcdefgh": 3}, 1, short) == "M/Abcd (2)/x.mp3"

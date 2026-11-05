"""hiby_index.read_tags: what the HiBy index reads from each format (mutation survivors)."""

from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TYER
from mutagen.mp4 import MP4

from syncmusic.devices.HibyR1 import hiby_index as h
from tests.media import make_music


def test_mp3_id3(tmp_path: Path) -> None:
    p = make_music(tmp_path / "a.mp3")
    id3 = ID3()
    id3.add(TIT2(encoding=3, text=["Title", "Second title"]))
    id3.add(TALB(encoding=3, text=["Album"]))
    id3.add(TPE1(encoding=3, text=["A", "B", "a"]))
    id3.add(TPE2(encoding=3, text=["AA"]))
    id3.add(TCON(encoding=3, text=["Rock", "Pop"]))
    id3.add(TRCK(encoding=3, text=["7/11"]))
    id3.add(TPOS(encoding=3, text=["2/2"]))
    id3.add(TDRC(encoding=3, text=["2003-05-01"]))
    id3.save(p, v2_version=4)
    t = h.read_tags(p)
    assert (t.title, t.album, t.artist, t.album_artist) == ("Title", "Album", "A; B", "AA")
    assert (t.genre, t.track, t.disc, t.year) == ("Rock; Pop", 7, 2, 2003)
    assert (t.sample_rate, t.bits, t.alac) == (0, 0, False)  # MP3: no rate/depth columns


def test_mp3_year_falls_back_to_tyer(tmp_path: Path) -> None:
    p = make_music(tmp_path / "a.mp3")
    id3 = ID3()
    id3.add(TYER(encoding=3, text=["1999"]))
    id3.save(p, v2_version=3)
    t = h.read_tags(p)
    assert (t.year, t.title, t.track, t.disc) == (1999, "", 0, 0)


def test_flac_vorbis(tmp_path: Path) -> None:
    p = make_music(tmp_path / "a.flac", sample_rate=48000, bit_depth=24)
    f = FLAC(p)
    f.delete()
    f["TITLE"] = ["T1", "T2"]
    f["ALBUM"] = "Al"
    f["ARTIST"] = ["X", "Y"]
    f["ALBUM ARTIST"] = "first"
    f["ALBUMARTIST"] = "second"
    f["GENRE"] = ["Jazz", "jazz"]
    f["TRACK"] = "3"
    f["DISCNUMBER"] = "1/2"
    f["YEAR"] = "1987"
    f.save()
    t = h.read_tags(p)
    assert (t.title, t.album, t.artist, t.genre) == ("T1", "Al", "X; Y", "Jazz")
    assert t.album_artist == "first"  # the last key in the reader's list that is present wins
    assert (t.track, t.disc, t.year) == (3, 1, 1987)
    assert (t.sample_rate, t.bits) == (48000, 24)


def test_m4a_alac(tmp_path: Path) -> None:
    p = make_music(tmp_path / "a.m4a", sample_rate=48000, bit_depth=24)
    m = MP4(p)
    m["\xa9nam"] = ["N"]
    m["\xa9alb"] = ["L"]
    m["\xa9ART"] = ["P", "Q"]
    m["aART"] = ["R", "S"]
    m["\xa9gen"] = ["G"]
    m["trkn"] = [(4, 9)]
    m["disk"] = [(2, 3)]
    m["\xa9day"] = ["2010-01-01"]
    m.save()
    t = h.read_tags(p)
    assert (t.title, t.album, t.artist, t.album_artist, t.genre) == ("N", "L", "P; Q", "R", "G")
    assert (t.track, t.disc, t.year) == (4, 2, 2010)
    assert t.alac and t.sample_rate == 48000 and t.bits == 24


def test_wav_rate_and_depth(tmp_path: Path) -> None:
    t = h.read_tags(make_music(tmp_path / "a.wav", sample_rate=48000, bit_depth=16))
    assert (t.sample_rate, t.bits, t.title) == (48000, 16, "")


def test_unreadable_and_unknown_files(tmp_path: Path) -> None:
    bad = tmp_path / "bad.flac"
    bad.write_bytes(b"fLaC" + b"\0" * 10)
    assert h.read_tags(bad) == h.Tags()
    junk = tmp_path / "x.mp3"
    junk.write_bytes(b"")
    assert h.read_tags(junk) == h.Tags()

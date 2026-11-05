"""Every original tag survives a conversion: sources rich in tags, written with each format's own frames /
atoms / comments (not through tagmap), go through the transcode processor to every target it supports; the output
has every field (under its mapped name), the cover, totals and multi-values. Retag only adds on top."""

from __future__ import annotations

import base64
import shutil

import mutagen
import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    COMM,
    ID3,
    TALB,
    TBPM,
    TCMP,
    TCOM,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TPUB,
    TRCK,
    TSO2,
    TSOA,
    TSOP,
    TSOT,
    TSRC,
    TXXX,
    UFID,
    USLT,
)
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.processors import tagmap
from syncmusic.processors.retag import Retag
from syncmusic.processors.transcode import Transcode
from tests.media import TINY_JPEG, make_music, media_work_item

ALL = DeviceCapabilities(formats={"mp3", "aac", "alac", "flac", "opus", "ogg"})

MB = {
    "musicbrainz_trackid": "6a5a2b6e-6f0e-4b5a-9a9a-000000000001",
    "musicbrainz_albumid": "6a5a2b6e-6f0e-4b5a-9a9a-000000000002",
    "musicbrainz_artistid": "6a5a2b6e-6f0e-4b5a-9a9a-000000000003",
    "musicbrainz_albumartistid": "6a5a2b6e-6f0e-4b5a-9a9a-000000000004",
    "musicbrainz_releasegroupid": "6a5a2b6e-6f0e-4b5a-9a9a-000000000005",
}
EXPECTED: dict[str, list[str]] = {
    "title": ["Зозуля"],
    "artist": ["Скрябін", "Андрій Кузьменко"],
    "album": ["Зірка на ймення Сонце"],
    "albumartist": ["Скрябін"],
    "tracknumber": ["7"],
    "tracktotal": ["11"],
    "discnumber": ["1"],
    "disctotal": ["2"],
    "date": ["1989"],
    "genre": ["Rock", "Post-Punk"],
    "composer": ["Андрій Кузьменко"],
    "lyrics": ["Пісень, ще не написаних, скільки?"],
    "bpm": ["120"],
    "compilation": ["1"],
    "comment": ["remaster"],
    "titlesort": ["Zozulya"],
    "artistsort": ["Skryabin"],
    "albumsort": ["Zirka"],
    "albumartistsort": ["Skryabin"],
    "isrc": ["SUA018900007"],
    "label": ["Мелодія"],
    "replaygain_track_gain": ["-7.20 dB"],
    "replaygain_track_peak": ["0.988"],
    **{k: [v] for k, v in MB.items()},
}


def _vorbis(f) -> None:
    for key, values in EXPECTED.items():
        f[key.upper()] = values


def _flac(path):
    f = FLAC(make_music(path))
    f.delete()
    _vorbis(f)
    pic = Picture()
    pic.data, pic.mime, pic.type = TINY_JPEG, "image/jpeg", 3
    f.add_picture(pic)
    f.save()
    return path


def _opus(path):
    f = OggOpus(make_music(path))
    f.delete()
    _vorbis(f)
    pic = Picture()
    pic.data, pic.mime, pic.type = TINY_JPEG, "image/jpeg", 3
    f["METADATA_BLOCK_PICTURE"] = [base64.b64encode(pic.write()).decode()]
    f.save()
    return path


def _mp3(path):
    make_music(path)
    id3 = ID3()
    e = EXPECTED
    for frame, key in [
        (TIT2, "title"), (TPE1, "artist"), (TALB, "album"), (TPE2, "albumartist"), (TDRC, "date"),
        (TCON, "genre"), (TCOM, "composer"), (TBPM, "bpm"), (TCMP, "compilation"), (TSOT, "titlesort"),
        (TSOP, "artistsort"), (TSOA, "albumsort"), (TSO2, "albumartistsort"), (TSRC, "isrc"), (TPUB, "label"),
    ]:  # fmt: skip
        id3.add(frame(encoding=3, text=e[key]))
    id3.add(TRCK(encoding=3, text=["7/11"]))
    id3.add(TPOS(encoding=3, text=["1/2"]))
    id3.add(COMM(encoding=3, lang="eng", desc="", text=e["comment"]))
    id3.add(USLT(encoding=3, lang="rus", desc="", text=e["lyrics"][0]))
    id3.add(UFID(owner="http://musicbrainz.org", data=MB["musicbrainz_trackid"].encode()))
    for desc, key in [
        ("MusicBrainz Album Id", "musicbrainz_albumid"), ("MusicBrainz Artist Id", "musicbrainz_artistid"),
        ("MusicBrainz Album Artist Id", "musicbrainz_albumartistid"),
        ("MusicBrainz Release Group Id", "musicbrainz_releasegroupid"),
        ("REPLAYGAIN_TRACK_GAIN", "replaygain_track_gain"), ("REPLAYGAIN_TRACK_PEAK", "replaygain_track_peak"),
    ]:  # fmt: skip
        id3.add(TXXX(encoding=3, desc=desc, text=e[key]))
    id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=TINY_JPEG))
    id3.save(path, v2_version=4)
    return path


def _m4a(path, codec):
    wav = make_music(path.with_suffix(".wav"))
    import subprocess

    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(wav),
            "-c:a",
            codec,
            "-fflags",
            "+bitexact",
            str(path),
        ],
        check=True,
    )
    f = MP4(path)
    e = EXPECTED
    for atom, key in [
        ("©nam", "title"), ("©ART", "artist"), ("©alb", "album"), ("aART", "albumartist"), ("©day", "date"),
        ("©gen", "genre"), ("©wrt", "composer"), ("©lyr", "lyrics"), ("©cmt", "comment"), ("sonm", "titlesort"),
        ("soar", "artistsort"), ("soal", "albumsort"), ("soaa", "albumartistsort"),
    ]:  # fmt: skip
        f[atom] = e[key]
    f["trkn"] = [(7, 11)]
    f["disk"] = [(1, 2)]
    f["tmpo"] = [120]
    f["cpil"] = True
    for name, key in [
        ("MusicBrainz Track Id", "musicbrainz_trackid"), ("MusicBrainz Album Id", "musicbrainz_albumid"),
        ("MusicBrainz Artist Id", "musicbrainz_artistid"), ("MusicBrainz Album Artist Id", "musicbrainz_albumartistid"),
        ("MusicBrainz Release Group Id", "musicbrainz_releasegroupid"), ("ISRC", "isrc"), ("LABEL", "label"),
        ("replaygain_track_gain", "replaygain_track_gain"), ("replaygain_track_peak", "replaygain_track_peak"),
    ]:  # fmt: skip
        f[f"----:com.apple.iTunes:{name}"] = [MP4FreeForm(v.encode()) for v in e[key]]
    f["covr"] = [MP4Cover(TINY_JPEG, imageformat=MP4Cover.FORMAT_JPEG)]
    f.save()
    return path


SOURCES = {
    "flac": lambda p: _flac(p / "src.flac"),
    "mp3": lambda p: _mp3(p / "src.mp3"),
    "aac": lambda p: _m4a(p / "src.m4a", "aac"),
    "alac": lambda p: _m4a(p / "src.m4a", "alac"),
    "opus": lambda p: _opus(p / "src.opus"),
}
PAIRS = [
    ("flac", "alac"),
    ("flac", "aac"),
    ("flac", "mp3"),
    ("flac", "opus"),
    ("flac", "ogg"),
    ("alac", "flac"),
    ("alac", "mp3"),
    ("aac", "mp3"),
    ("mp3", "aac"),
    ("mp3", "opus"),
    ("opus", "mp3"),
    ("opus", "aac"),
]


@pytest.mark.parametrize("fmt", sorted(SOURCES))
def test_read_every_source_format(tmp_path, fmt):
    got = tagmap.read(SOURCES[fmt](tmp_path))
    assert got.tags == EXPECTED and got.cover == TINY_JPEG


@pytest.mark.parametrize(("src_fmt", "to"), PAIRS)
async def test_conversion_keeps_every_tag(tmp_path, src_fmt, to):
    src = SOURCES[src_fmt](tmp_path / "src")
    p = Transcode(
        {"rules": [{"from": [src_fmt], "to": to, "when": "always"}]},
        None,
    )
    item = media_work_item(src, tmp_path / "work")
    out = await p.run(item.model_copy(update={"plan": p.plan(item.plan, ALL)}), ALL)
    assert out.path != src and out.notes["transcode"].endswith(to)
    got = tagmap.read(out.path)
    missing = {k: v for k, v in EXPECTED.items() if got.tags.get(k) != v}
    assert missing == {}, f"{src_fmt}→{to} lost or changed: {sorted(missing)}; got {got.tags}"
    assert got.cover == TINY_JPEG


def test_on_disk_names_follow_picard(tmp_path):
    """Other players and taggers find the fields: Picard's TXXX / freeform names, MP4 number pairs, Vorbis totals."""
    data = tagmap.read(_flac(tmp_path / "s.flac"))
    mp3 = make_music(tmp_path / "o.mp3")
    tagmap.write(mp3, data)
    id3 = ID3(mp3)
    assert id3.getall("UFID")[0].data.decode() == MB["musicbrainz_trackid"]
    assert id3["TXXX:MusicBrainz Album Id"].text == [MB["musicbrainz_albumid"]]
    assert id3["TXXX:replaygain_track_gain"].text == ["-7.20 dB"]
    assert id3["TRCK"].text == ["7/11"] and id3["TPOS"].text == ["1/2"]
    assert id3.getall("USLT")[0].text == EXPECTED["lyrics"][0] and id3["TCMP"].text == ["1"]
    assert id3.getall("APIC")[0].data == TINY_JPEG
    m4a = tmp_path / "o.m4a"
    shutil.copy(_m4a(tmp_path / "x.m4a", "aac"), m4a)
    tagmap.write(m4a, data)
    f = MP4(m4a)
    assert (
        f["trkn"] == [(7, 11)]
        and f["disk"] == [(1, 2)]
        and f["tmpo"] == [120]
        and f["cpil"] is True
    )
    assert (
        bytes(f["----:com.apple.iTunes:MusicBrainz Track Id"][0]).decode()
        == MB["musicbrainz_trackid"]
    )
    assert f["©ART"] == ["Скрябін", "Андрій Кузьменко"]
    opus = make_music(tmp_path / "o.opus")
    tagmap.write(opus, data)
    f = mutagen.File(opus)
    assert f["TRACKTOTAL"] == ["11"] and f["ARTIST"] == ["Скрябін", "Андрій Кузьменко"]
    assert "METADATA_BLOCK_PICTURE" in f


async def test_retag_puts_library_values_on_top_and_removes_nothing(tmp_path):
    """After a conversion retag writes the library's view; every other original field stays (also MusicBrainz's
    UFID with strip_private on)."""
    src = _flac(tmp_path / "src" / "s.flac")
    p = Transcode(
        {"rules": [{"from": ["flac"], "to": "mp3", "when": "always"}]},
        None,
    )
    item = media_work_item(src, tmp_path / "work")
    out = await p.run(item.model_copy(update={"plan": p.plan(item.plan, ALL)}), ALL)
    track = out.plan.track.model_copy(update={"title": "Зозуля (remaster)", "genres": ["Rock"]})
    out = out.model_copy(update={"plan": out.plan.model_copy(update={"track": track})})
    r = Retag({"strip_private": True}, None)
    done = await r.run(out, ALL)
    got = tagmap.read(done.path).tags
    assert got["title"] == ["Зозуля (remaster)"] and got["genre"] == ["Rock"]
    for key in EXPECTED.keys() - {
        "title",
        "genre",
        "artist",
        "album",
        "albumartist",
        "date",
        "tracknumber",
        "discnumber",
    }:
        assert got.get(key) == EXPECTED[key], key
    assert tagmap.read(done.path).cover == TINY_JPEG


def test_plain_copy_keeps_the_file_untouched(tmp_path):
    """No conversion: the file is copied byte for byte - tagmap never runs on it."""
    src = _mp3(tmp_path / "s.mp3")
    before = src.read_bytes()
    item = media_work_item(src, tmp_path / "work")
    p = Transcode({"rules": [{"from": ["flac"], "to": "mp3"}]}, None)
    assert p.plan(item.plan, ALL).steps == item.plan.steps  # nothing planned for an MP3
    assert src.read_bytes() == before

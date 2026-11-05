"""retag processor: library metadata written per tag format, read back with mutagen."""

from __future__ import annotations

import datetime as dt

import mutagen
import pytest
from mutagen.id3 import ID3, PRIV
from mutagen.mp4 import MP4

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.core.models import MediaKind
from syncmusic.processors.retag import Retag, _date, _number
from tests.media import make_music, media_work_item

CAPS = DeviceCapabilities(formats={"mp3", "aac", "alac", "flac", "ogg", "opus"})


def item_for(tmp_path, name: str, **track):
    src = make_music(
        tmp_path / "src" / name, title="file title", artist="file artist", album="Альбом"
    )
    item = media_work_item(src, tmp_path / "work")
    t = item.track.model_copy(
        update={
            "title": "Група крові",
            "artist": "Скрябін",
            "album": "Група крові",
            "track_no": 1,
            "disc_no": 1,
            "year": 1988,
            "genres": ["Rock", "Post-Punk"],
            **track,
        }
    )
    return item.model_copy(update={"plan": item.plan.model_copy(update={"track": t})})


async def run(tmp_path, name, settings=None, **track):
    item = item_for(tmp_path, name, **track)
    src_bytes = item.path.read_bytes()
    out = await Retag(settings or {}, None).run(item, CAPS)
    assert item.path.read_bytes() == src_bytes  # the original is never modified
    assert out.path.parent == item.work_dir and out.steps_done == ["retag"]
    assert out.plan.file.size == out.path.stat().st_size
    return out


@pytest.mark.parametrize("name", ["a.mp3", "a.m4a", "a.flac", "a.opus"])
async def test_library_metadata_written(tmp_path, name):
    out = await run(tmp_path, name)
    easy = mutagen.File(out.path, easy=True)
    if name.endswith(".m4a"):  # EasyMP4 knows the basic keys
        tags = MP4(out.path).tags
        assert tags["©nam"] == ["Група крові"] and tags["aART"] == ["Скрябін"]
        assert tags["trkn"][0][0] == 1 and tags["disk"][0][0] == 1
        assert tags["©gen"] == ["Rock", "Post-Punk"] and tags["©day"] == ["1988"]
        assert "cpil" not in tags  # not a compilation: nothing written, nothing removed
        return
    assert easy["title"] == ["Група крові"]
    assert easy["artist"] == ["Скрябін"]
    assert easy["album"] == ["Група крові"]
    assert easy["albumartist"] == ["Скрябін"]  # fallback
    assert easy["tracknumber"] == ["1"]
    assert easy["discnumber"] == ["1"]
    assert str(easy["date"][0]) == "1988"
    assert easy["genre"] == ["Rock", "Post-Punk"]
    assert "compilation" not in easy


async def test_album_artist_fallback_off_keeps_file_value(tmp_path):
    out = await run(tmp_path, "a.flac", {"album_artist_fallback": False})
    assert "albumartist" not in mutagen.File(out.path, easy=True)


async def test_unknown_library_fields_keep_file_tags(tmp_path):
    out = await run(tmp_path, "a.mp3", album=None, year=None)
    easy = mutagen.File(out.path, easy=True)
    assert easy["album"] == ["Альбом"]  # library had no album: file value kept


@pytest.mark.parametrize("name", ["a.mp3", "a.m4a", "a.flac"])
async def test_compilation_flag(tmp_path, name):
    out = await run(tmp_path, name, compilation=True)
    if name.endswith(".m4a"):
        assert MP4(out.path).tags["cpil"] is True
    elif name.endswith(".mp3"):
        assert str(ID3(out.path)["TCMP"]) == "1"
    else:
        assert mutagen.File(out.path)["COMPILATION"] == ["1"]


async def test_id3v23_and_strip_private(tmp_path):
    item = item_for(tmp_path, "a.mp3")
    id3 = ID3(item.path)
    id3.add(PRIV(owner="AmazonJunk", data=b"\0" * 5000))
    id3.save()
    out = await Retag({"id3v23": True}, None).run(item, CAPS)
    id3 = ID3(out.path)
    assert id3.version[:2] == (2, 3)
    assert not id3.getall("PRIV")
    assert str(ID3(out.path, translate=False)["TYER"]) == "1988"  # v2.3 has no TDRC
    out = await Retag({"strip_private": False}, None).run(item, CAPS)
    assert ID3(out.path).getall("PRIV") and ID3(out.path).version[:2] == (2, 4)


async def test_rating_and_play_count(tmp_path):
    s = {"write_rating": True, "write_play_count": True}
    out = await run(tmp_path, "a.mp3", s, rating=4, play_count=7)
    popm = ID3(out.path).getall("POPM")[0]
    assert (popm.rating, popm.count) == (196, 7)
    out = await run(tmp_path, "a.flac", s, rating=5, play_count=2)
    f = mutagen.File(out.path)
    assert f["FMPS_RATING"] == ["1.0"] and f["FMPS_PLAYCOUNT"] == ["2"]
    out = await run(tmp_path, "a.m4a", s, rating=3, play_count=1)
    tags = MP4(out.path).tags
    assert bytes(tags["----:com.apple.iTunes:FMPS_Rating"][0]) == b"0.6"
    out = await run(tmp_path, "a.mp3")  # off by default
    assert not ID3(out.path).getall("POPM")


async def test_podcast_marker_mp4(tmp_path):
    out = await run(tmp_path, "a.m4a", kind=MediaKind.PODCAST)
    tags = MP4(out.path).tags
    assert tags["stik"] == [21] and tags["pcst"] is True
    out = await run(tmp_path, "a.m4a", kind=MediaKind.AUDIOBOOK)
    assert MP4(out.path).tags["stik"] == [2]


async def test_templates(tmp_path):
    s = {"templates": {"title": "{track_no:02d}. {title} ({year})", "album": "{album} [{genre}]"}}
    out = await run(tmp_path, "a.flac", s)
    easy = mutagen.File(out.path, easy=True)
    assert easy["title"] == ["01. Група крові (1988)"]
    assert easy["album"] == ["Група крові [Rock]"]


@pytest.mark.parametrize(
    "templates",
    [
        {"title": "{title.__class__}"},
        {"title": "{nope}"},
        {"path": "{title}"},
        {"title": "{title!r}"},
        {"title": "{title:>{year}}"},
    ],
)
def test_bad_templates_rejected(templates):
    with pytest.raises(ValueError):
        Retag({"templates": templates}, None)


async def test_work_file_modified_in_place(tmp_path):
    item = item_for(tmp_path, "a.flac")
    owned = item.work_dir / "x.flac"
    owned.write_bytes(item.path.read_bytes())
    out = await Retag({}, None).run(item.model_copy(update={"path": owned}), CAPS)
    assert out.path == owned


async def test_corrupt_file_passes_through(tmp_path):
    item = item_for(tmp_path, "a.mp3")
    bad = tmp_path / "bad.flac"
    bad.write_bytes(b"fLaC garbage")
    item = item.model_copy(update={"path": bad})
    assert await Retag({}, None).run(item, CAPS) == item


def test_suggest():
    assert Retag.suggest(CAPS).type == "retag"
    # iPods too: the files carry the tags even though the iPod shows its iTunesDB
    assert Retag.suggest(CAPS.model_copy(update={"needs_device_db": True})).type == "retag"


@pytest.mark.parametrize("name", ["t.mp3", "t.flac", "t.m4a"])
async def test_retag_keeps_totals_and_full_date(tmp_path, name):
    """Library fields hold less than the file ('2/17', a full date): retag must not throw that away."""
    src = make_music(
        tmp_path / "src" / name, title="x", tracknumber="2/17", discnumber="1/2", date="2011-03-04"
    )
    item = media_work_item(src, tmp_path / "work")
    t = item.plan.track.model_copy(update={"track_no": 2, "disc_no": 1, "year": 2011})
    item = item.model_copy(update={"plan": item.plan.model_copy(update={"track": t})})
    out = await Retag({}, None).run(item, CAPS)
    easy = mutagen.File(out.path, easy=True)
    assert easy["tracknumber"] == ["2/17"]
    assert easy["discnumber"] == ["1/2"]
    assert easy["date"] == ["2011-03-04"]


@pytest.mark.parametrize(
    ("new", "old", "want"),
    [
        (2, "2/17", "2/17"),
        (3, "2/17", "3/17"),
        (18, "2/17", "18"),
        (2, "2", "2"),
        (2, None, "2"),
        (2, "2/x", "2"),
    ],
)
def test_number_keeps_total(new, old, want):
    assert _number(new, old) == want


@pytest.mark.parametrize(
    ("year", "old", "want"),
    [
        (2011, "2011-03-04", "2011-03-04"),
        (2012, "2011-03-04", "2012"),
        (2011, "2011", "2011"),
        (2011, None, "2011"),
    ],
)
def test_date_keeps_full_date_of_same_year(year, old, want):
    assert _date(year, old) == want


async def test_retag_v23_keeps_full_date_and_total(tmp_path):
    src = make_music(tmp_path / "src" / "t.mp3", title="x", tracknumber="2/17", date="2011-03-04")
    item = media_work_item(src, tmp_path / "work")
    t = item.plan.track.model_copy(update={"track_no": 2, "year": 2011})
    item = item.model_copy(update={"plan": item.plan.model_copy(update={"track": t})})
    out = await Retag({"id3v23": True}, None).run(item, CAPS)
    id3 = ID3(
        out.path, translate=False
    )  # raw v2.3 frames (mutagen upgrades to v2.4 on load otherwise)
    assert id3.version[1] == 3
    assert str(id3["TRCK"].text[0]) == "2/17"
    assert str(id3["TYER"].text[0]) == "2011"
    assert str(id3["TDAT"].text[0]) == "0403"


@pytest.mark.parametrize("name", ["a.mp3", "a.m4a", "a.flac"])
async def test_episode_release_date_is_written_as_a_full_date(tmp_path, name):
    """a podcast episode's publish date goes into the file, not only the year."""
    published = dt.datetime(2024, 3, 5, 12, 0, tzinfo=dt.UTC)
    out = await run(tmp_path, name, kind=MediaKind.PODCAST, year=2024, published=published)
    if name.endswith(".m4a"):
        assert MP4(out.path).tags["©day"] == ["2024-03-05"]
    elif name.endswith(".mp3"):
        id3 = ID3(out.path)
        assert str(id3["TDRL"]) == "2024-03-05" and str(id3["TDRC"]) == "2024-03-05"
    else:
        assert mutagen.File(out.path)["DATE"] == ["2024-03-05"]


async def test_music_keeps_the_year(tmp_path):
    published = dt.datetime(2024, 3, 5, 12, 0, tzinfo=dt.UTC)
    out = await run(tmp_path, "a.mp3", year=2024, published=published)  # music: no release date
    assert "TDRL" not in ID3(out.path)

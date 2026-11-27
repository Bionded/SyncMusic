"""The audio identity stays when only the tags change and moves when the audio does."""

from __future__ import annotations

import shutil

import mutagen
import pytest

from syncmusic.core.audio_identity import audio_identity, file_identity
from tests.media import big_cover, embed_cover, make_music, make_track

FORMATS = [".flac", ".mp3", ".m4a", ".opus"]


def _retag(path, **tags) -> None:
    audio = mutagen.File(path, easy=True)
    if audio.tags is None:
        audio.add_tags()
    for k, v in tags.items():
        audio.tags[k] = v
    audio.save()


@pytest.mark.parametrize("ext", FORMATS)
async def test_retag_keeps_the_identity_and_new_audio_changes_it(tmp_path, ext):
    a = make_music(tmp_path / f"a{ext}", title="Без назви", artist="Гурт")
    ident = await file_identity(a)
    assert ident is not None
    size = a.stat().st_size

    _retag(a, title="Нова назва " * 50, album="Альбом")
    embed_cover(a, big_cover(200, 200))
    assert a.stat().st_size != size
    assert await file_identity(a) == ident

    other = make_track(tmp_path / f"b{ext}", seconds=2.0, title="Без назви")
    assert await file_identity(other) not in (None, ident)


async def test_only_small_ranges_are_read(tmp_path):
    a = make_music(tmp_path / "a.flac", seconds=10.0)
    data = a.read_bytes()
    asked: list[int] = []

    async def read(offset, length):
        asked.append(length)
        return data[offset : offset + length]

    assert await audio_identity(read, len(data)) == await file_identity(a)
    assert sum(asked) < 1024


async def test_flac_without_md5_falls_back_to_the_audio_slice(tmp_path):
    a = make_music(tmp_path / "a.flac")
    data = bytearray(a.read_bytes())
    data[26:42] = bytes(16)  # STREAMINFO MD5 unset
    a.write_bytes(bytes(data))
    ident = await file_identity(a)
    assert ident is not None and ident.startswith("flac~:")
    b = tmp_path / "b.flac"
    shutil.copyfile(a, b)
    _retag(b, title="x" * 3000)
    assert await file_identity(b) == ident


async def test_unknown_or_broken_files_have_none(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"not audio at all" * 10)
    assert await file_identity(p) is None
    q = tmp_path / "y.flac"
    q.write_bytes(b"fLaC\x00\x00")
    assert await file_identity(q) is None


async def test_subsonic_library_reads_the_identity_with_range_requests(tmp_path):
    from syncmusic.core.models import LibraryConfig
    from syncmusic.libraries.subsonic import SubsonicLibrary
    from tests.fake_navidrome import FakeNavidrome, song

    path = make_music(tmp_path / "a.flac", seconds=10.0, title="Пісня")
    nd = FakeNavidrome(native=False)
    s = song("s1", "Пісня")
    s["_data"] = path.read_bytes()
    s["size"] = len(s["_data"])
    s["contentType"] = "audio/flac"
    nd.add(s)
    lib = SubsonicLibrary(
        LibraryConfig(
            id="nd",
            type="navidrome",
            name="N",
            settings={"url": "http://nd.test", "username": "admin", "password": "pw"},
        ),
        None,
    )
    lib.transport = nd
    try:
        assert await lib.audio_identity("s1", s["size"]) == await file_identity(path)
        assert 0 < nd.download_bytes < 1024
    finally:
        await lib.close()


async def test_folder_library_reads_the_identity_locally(tmp_path):
    from syncmusic.core.models import LibraryConfig
    from syncmusic.libraries.folder import FolderLibrary

    make_music(tmp_path / "lib" / "a.mp3", title="Пісня")
    lib = FolderLibrary(
        LibraryConfig(id="f", type="folder", name="F", settings={"root": str(tmp_path / "lib")}),
        None,
    )
    snap = await lib.snapshot()
    (t,) = snap.tracks.values()
    assert await lib.audio_identity(t.ref.item_id, t.size_bytes) == await file_identity(
        tmp_path / "lib" / "a.mp3"
    )

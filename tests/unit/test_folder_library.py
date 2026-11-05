"""Folder library on synthetic media."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import pytest

from syncmusic.core.models import LibraryConfig, Lyrics, MediaKind
from syncmusic.libraries.folder import FolderLibrary
from tests.media import TINY_JPEG, add_synced_lyrics_id3, make_track


def lib_for(root: Path, **settings) -> FolderLibrary:
    return FolderLibrary(
        LibraryConfig(id="f", type="folder", name="F", settings={"root": str(root), **settings}),
        None,
    )


def by_path(snap):
    return {t.path: t for t in snap.tracks.values()}


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "lib"
    make_track(
        r / "Music" / "Скрябін (1988)" / "01 - Група крові.mp3",
        title="Група крові",
        artist="Скрябін",
        albumartist="Скрябін",
        album="Група крові",
        tracknumber="1/8",
        discnumber="1/1",
        date="1988-01-05",
        genre="Rock;Post-Punk",
        cover=True,
    )
    make_track(
        r / "Music" / "Скрябін (1988)" / "02 - Зачини за мною двері.flac",
        title="Зачини за мною двері",
        artist="Скрябін",
        album="Група крові",
        tracknumber="2",
        lyrics="[00:01.50]Зачини за мною двері\n[01:00.00]я йду",
    )
    make_track(r / "Music" / "Untagged's, (x).m4a")
    (r / "Music" / "Скрябін (1988)" / "02 - Зачини за мною двері.xml").write_text("<details/>")
    (r / "Music" / "notes.txt").write_text("not media")
    for name in ("Book - 24. Ch three.mp3", "Book - 13. Ch one.mp3", "Book - 14. Ch two.mp3"):
        make_track(r / "Books" / "Book" / name, title=name.split(". ")[1].removesuffix(".mp3"))
    make_track(r / "Books" / "Book" / "Book. Epilogue.mp3", title="Epilogue")
    make_track(
        r / "Pods" / "Show" / "ep.m4a",
        title="Ep",
        album="Show",
        date="Tue, 10 Jun 2025 17:27:28 GMT",
    )
    (r / "Pods" / "Show" / "Cover.JPG").write_bytes(TINY_JPEG)
    (r / ".hidden").mkdir()
    make_track(r / ".hidden" / "x.mp3", title="hidden")
    return r


@pytest.fixture
def lib(root):
    return lib_for(root, subfolder_kinds={"Books": "audiobook", "Pods": "podcast"})


async def test_snapshot_tags_and_kinds(lib):
    tracks = by_path(await lib.snapshot())
    assert len(tracks) == 8  # .xml, .txt and hidden folders ignored
    t = tracks["Music/Скрябін (1988)/01 - Група крові.mp3"]
    assert (t.title, t.artist, t.album, t.track_no, t.disc_no, t.year) == (
        "Група крові",
        "Скрябін",
        "Група крові",
        1,
        1,
        1988,
    )
    assert t.genres == ["Rock", "Post-Punk"] and t.format == "mp3" and t.kind is MediaKind.MUSIC
    assert t.has_cover and not t.has_lyrics and t.album_id and t.artist_id
    assert t.size_bytes == (lib.root / t.path).stat().st_size and t.duration_s == pytest.approx(
        1.0, abs=0.1
    )
    flac = tracks["Music/Скрябін (1988)/02 - Зачини за мною двері.flac"]
    assert flac.format == "flac" and flac.has_lyrics and not flac.has_cover and flac.bit_depth == 16
    assert flac.album_id == t.album_id  # same album artist|album… album_artist missing → artist
    bare = tracks["Music/Untagged's, (x).m4a"]
    assert bare.title == "Untagged's, (x)" and bare.format == "aac" and bare.artist is None


async def test_ids_are_sha1_of_relative_path_and_stable(lib, root, tmp_path):
    a = await lib.snapshot()
    other = lib_for(root, subfolder_kinds={"Books": "audiobook", "Pods": "podcast"})
    assert set((await other.snapshot()).tracks) == set(a.tracks)
    import hashlib

    t = next(iter(a.tracks.values()))
    assert t.ref.item_id == hashlib.sha1(t.path.encode()).hexdigest()


async def test_audiobook_chapter_order_and_podcast(lib):
    tracks = (await lib.snapshot()).tracks.values()
    book = sorted(
        (t for t in tracks if t.kind is MediaKind.AUDIOBOOK), key=lambda t: t.chapter_index
    )
    # natural file-name order (13 < 14 < 24); "Book. Epilogue" sorts after "Book - …" ('.' > '-')
    assert [t.title for t in book] == ["Ch one", "Ch two", "Ch three", "Epilogue"]
    assert {t.series for t in book} == {"Book"}
    pod = next(t for t in tracks if t.kind is MediaKind.PODCAST)
    assert (
        pod.series == "Show" and pod.published.year == 2025 and pod.has_cover
    )  # Cover.JPG sidecar


async def test_track_numbers_win_over_file_names(tmp_path):
    for n, name in [(2, "a.mp3"), (1, "b.mp3"), (3, "c.mp3")]:
        make_track(tmp_path / "B" / name, title=name, tracknumber=str(n))
    lib = lib_for(tmp_path, default_kind="audiobook")
    book = sorted((await lib.snapshot()).tracks.values(), key=lambda t: t.chapter_index)
    assert [t.title for t in book] == ["b.mp3", "a.mp3", "c.mp3"]


async def test_lyrics_and_cover(lib, root):
    tracks = by_path(await lib.snapshot())
    flac = tracks["Music/Скрябін (1988)/02 - Зачини за мною двері.flac"]
    lyr = await lib.get_lyrics(flac.ref.item_id)
    assert lyr.synced and lyr.lines == [(1.5, "Зачини за мною двері"), (60.0, "я йду")]
    mp3 = tracks["Music/Скрябін (1988)/01 - Група крові.mp3"]
    assert await lib.get_lyrics(mp3.ref.item_id) is None
    assert await lib.open_cover(mp3.ref.item_id) == TINY_JPEG
    assert await lib.open_cover(flac.ref.item_id) is None
    # a sidecar .lrc wins and is picked up on rescan (cache key includes sidecars)
    (root / "Music" / "Скрябін (1988)" / "01 - Група крові.lrc").write_text(
        "[00:02.00]sidecar", encoding="utf-8"
    )
    mp3 = by_path(await lib.snapshot())[mp3.path]
    assert mp3.has_lyrics
    assert (await lib.get_lyrics(mp3.ref.item_id)).lines == [(2.0, "sidecar")]


async def test_sylt_and_plain_embedded_lyrics(tmp_path):
    p = make_track(tmp_path / "s.mp3", title="s")
    add_synced_lyrics_id3(p, [("one", 1000), ("two", 2500)])
    make_track(tmp_path / "u.m4a", title="u", lyrics="just words")
    lib = lib_for(tmp_path)
    tracks = by_path(await lib.snapshot())
    assert (await lib.get_lyrics(tracks["s.mp3"].ref.item_id)).lines == [(1.0, "one"), (2.5, "two")]
    plain = await lib.get_lyrics(tracks["u.m4a"].ref.item_id)
    assert not plain.synced and plain.text == "just words"


async def test_rescan_reuses_cache_and_detects_changes(lib, root, monkeypatch):
    a = await lib.snapshot()
    calls = []
    orig = lib._read_track
    monkeypatch.setattr(lib, "_read_track", lambda *args: calls.append(args[0]) or orig(*args))
    b = await lib.snapshot()
    assert calls == [] and a.state_token == b.state_token and a.tracks == b.tracks
    target = root / "Music" / "Untagged's, (x).m4a"
    make_track(target, title="Now tagged")
    st = target.stat()
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    c = await lib.snapshot()
    assert calls == ["Music/Untagged's, (x).m4a"] and c.state_token != b.state_token
    assert by_path(c)["Music/Untagged's, (x).m4a"].title == "Now tagged"
    shutil.rmtree(root / "Pods")
    assert len((await lib.snapshot()).tracks) == 7


async def test_unreadable_file_is_skipped(tmp_path):
    make_track(tmp_path / "ok.mp3", title="ok")
    (tmp_path / "broken.flac").write_bytes(b"not a flac at all")
    assert [t.title for t in (await lib_for(tmp_path).snapshot()).tracks.values()] == ["ok"]


async def test_check_and_open_media(tmp_path, lib):
    assert (await lib.check()).ok
    missing = lib_for(tmp_path / "nope")
    status = await missing.check()
    assert not status.ok and "does not exist" in status.message
    with pytest.raises(KeyError):
        await lib.open_media("unknown")
    snap = await lib.snapshot()
    t = next(iter(snap.tracks.values()))
    assert await lib.open_media(t.ref.item_id) == (lib.root / t.path).resolve()


def test_lyrics_from_text():
    lyr = Lyrics.from_text("[ar:X]\n[00:01.00][00:03.00]a\n[00:02:50]b\n")
    assert lyr.synced and lyr.lines == [(1.0, "a"), (2.5, "b"), (3.0, "a")]
    assert Lyrics.from_text("hello\nworld\n") == Lyrics(synced=False, text="hello\nworld")


async def test_non_utf8_file_name_does_not_break_snapshot(tmp_path):
    # e.g. a zip of Cyrillic media packed with cp1251 names: one bad name must not abort the whole scan
    r = tmp_path / "lib"
    make_track(r / "ok.mp3", title="ok")
    bad = os.fsencode(str(r)) + b"/" + "Скрябін".encode("cp1251") + b".mp3"
    shutil.copyfile(r / "ok.mp3", bad)
    snap = await lib_for(r).snapshot()
    assert "ok.mp3" in by_path(snap)
    assert len(snap.tracks) in (1, 2)  # skipping undecodable names is fine, crashing is not


async def test_missing_root_snapshot_raises(tmp_path):
    # An unmounted music disk must never look like an empty library (the planner would delete everything).
    with pytest.raises((FileNotFoundError, OSError, RuntimeError)):
        await lib_for(tmp_path / "not-mounted").snapshot()


async def test_non_utf8_file_name_changes_state_token(tmp_path):
    r = tmp_path / "lib"
    make_track(r / "ok.mp3", title="ok")
    lib = lib_for(r)
    t0 = await lib.state_token()
    bad = os.fsencode(str(r)) + b"/" + "Скрябін".encode("cp1251") + b".mp3"
    shutil.copyfile(r / "ok.mp3", bad)
    t1 = await lib.state_token()
    assert t1 != t0
    os.rename(bad, bad.replace(b".mp3", b"2.mp3"))
    assert await lib.state_token() != t1


async def test_missing_root_state_token_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        await lib_for(tmp_path / "not-mounted").state_token()


async def test_sidecar_changes_change_source_state(lib, root):
    """.lrc and folder cover changes must reach the device (source_state feeds plan + cache key)."""
    album = root / "Music" / "Скрябін (1988)"
    mp3, flac = (
        "Music/Скрябін (1988)/01 - Група крові.mp3",
        "Music/Скрябін (1988)/02 - Зачини за мною двері.flac",
    )
    pod = "Pods/Show/ep.m4a"

    async def states():
        return {p: t.source_state for p, t in by_path(await lib.snapshot()).items()}

    def bump(p: Path) -> None:
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    s0 = await states()
    assert await states() == s0  # stable across rescans
    lrc = album / "01 - Група крові.lrc"
    lrc.write_text("[00:01.00]first\n", encoding="utf-8")  # added
    s1 = await states()
    assert {p for p in s0 if s0[p] != s1[p]} == {mp3}
    lrc.write_text("[00:01.00]second\n", encoding="utf-8")  # edited (same size: mtime differs)
    bump(lrc)
    s2 = await states()
    assert {p for p in s1 if s1[p] != s2[p]} == {mp3}
    lrc.unlink()  # removed
    assert (await states())[mp3] == s0[mp3]
    cover = root / "Pods" / "Show" / "Cover.JPG"
    cover.write_bytes(TINY_JPEG + b"\0")  # cover replaced
    s3 = await states()
    assert {p for p in s0 if s0[p] != s3[p]} == {pod}
    (album / "folder.jpg").write_bytes(TINY_JPEG)  # new folder cover: both album tracks
    s4 = await states()
    assert {p for p in s3 if s3[p] != s4[p]} == {mp3, flac}


async def test_m3u_playlists(lib, root):
    """.m3u / .m3u8 files are library playlists; relative, absolute-in-root and backslash entries resolve,
    URLs / outside / missing entries are left out; #PLAYLIST names it, else the file name."""
    (root / "Music" / "Скрябін.m3u8").write_text(
        "#EXTM3U\n#PLAYLIST:Найкраще\n#EXTINF:1,Скрябін - Група крові\n"
        "Скрябін (1988)/01 - Група крові.mp3\n"
        "Скрябін (1988)\\02 - Зачини за мною двері.flac\n"
        "http://example.com/stream.mp3\n../../outside.mp3\nmissing.mp3\n",
        encoding="utf-8",
    )
    (root / "Lists").mkdir()
    (root / "Lists" / "mix.m3u").write_bytes(
        f"{root}/Pods/Show/ep.m4a\n../Music/Untagged's, (x).m4a\n/elsewhere/a.mp3\n".encode(
            "latin-1"
        )
    )
    snap = await lib.snapshot()
    paths = {t.ref.key(): t.path for t in snap.tracks.values()}
    pls = {p.name: [paths[r.key()] for r in p.track_refs] for p in snap.playlists.values()}
    assert pls == {
        "Найкраще": [
            "Music/Скрябін (1988)/01 - Група крові.mp3",
            "Music/Скрябін (1988)/02 - Зачини за мною двері.flac",
        ],
        "mix": ["Pods/Show/ep.m4a", "Music/Untagged's, (x).m4a"],
    }
    assert all(len(t.path or "") and not t.path.endswith(".m3u") for t in snap.tracks.values())


async def test_m3u_missing_entries_logged(lib, root, caplog):
    """entries without a file in the library are left out with one warning per playlist and snapshot."""
    (root / "mix.m3u8").write_text(
        "Music/Скрябін (1988)/01 - Група крові.mp3\nmissing/nothere.mp3\n/elsewhere/a.mp3\n",
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="syncmusic.libraries.folder")
    for n in (1, 2):
        snap = await lib.snapshot()
        assert [len(p.track_refs) for p in snap.playlists.values()] == [1]
        lines = [r.getMessage() for r in caplog.records if "mix.m3u8" in r.getMessage()]
        assert len(lines) == n
        assert "missing/nothere.mp3" in lines[-1] and "/elsewhere/a.mp3" in lines[-1]
        assert "2 entries" in lines[-1]


async def test_media_type_of_the_library(tmp_path):
    """a folder library is music, audiobooks or podcasts (default_kind); subfolder_kinds still win."""
    root = tmp_path / "books"
    for name in ("02 Two.mp3", "01 One.mp3"):
        make_track(root / "Книга" / name, title=name[3:-4], album="Книга")
    snap = await lib_for(root, default_kind="audiobook").snapshot()
    assert {t.kind for t in snap.tracks.values()} == {MediaKind.AUDIOBOOK}
    assert sorted((t.chapter_index, t.title) for t in snap.tracks.values()) == [
        (1, "One"),
        (2, "Two"),
    ]
    assert {t.series for t in snap.tracks.values()} == {"Книга"}
    snap = await lib_for(root, default_kind="podcast").snapshot()
    assert {t.kind for t in snap.tracks.values()} == {MediaKind.PODCAST}
    snap = await lib_for(
        root, default_kind="podcast", subfolder_kinds={"Книга": "music"}
    ).snapshot()
    assert {t.kind for t in snap.tracks.values()} == {MediaKind.MUSIC}

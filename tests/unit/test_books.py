"""Multi-file audiobooks as one book with chapters on the stock iPod."""

# ruff: noqa: F811 - fixtures imported from test_position_roundtrip

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from pygpod import Database as IPodDB

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.core import books
from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import ActionKind, DeviceConfig, MediaKind, Rule, RuleSet
from syncmusic.core.planner import plan_sync
from syncmusic.core.prepare import Chain
from syncmusic.libraries.audiobookshelf import AudiobookshelfLibrary
from tests.factories import snapshot, track
from tests.fake_abs import FakeABS
from tests.media import make_track
from tests.unit.test_position_roundtrip import _sync, env  # noqa: F401 - fixture

ALL = Rule(id="all", criteria={"all": []})


def _chapters(**kw):
    common = dict(kind=MediaKind.AUDIOBOOK, album="Book", album_id="bk", author="Au", **kw)
    return [
        track("bk:2", title="Two", chapter_index=2, duration_s=30.0, position_s=0.0, **common),
        track("bk:1", title="One", chapter_index=1, duration_s=60.0, position_s=60.0, **common),
        track("solo:1", title="Solo", kind=MediaKind.AUDIOBOOK, album_id="solo"),
        track("m1", title="Song"),
    ]


def test_book_tracks_group_multi_file_books_in_chapter_order():
    snap = snapshot(_chapters())
    assert list(snap.books) == ["bk"]  # a single-file book and music stay as they are
    book = snap.books["bk"]
    assert book.title == "Book" and book.duration_s == 90.0 and book.position_s == 60.0
    assert [p.ref.item_id for p in books.parts_of(book)] == ["bk:1", "bk:2"]
    assert book.format == "aac" and book.path.endswith("Book.m4b")
    assert snap.track("bk") is book and snap.track("bk:1") is snap.tracks["bk:1"]
    assert "bk" not in snap.tracks  # rules and library views see the files only


def test_a_file_too_big_alone_keeps_its_book_as_files():
    big = books.MAX_BOOK_BYTES + 1
    assert not snapshot(_chapters(size_bytes=big)).books


def test_too_big_books_are_split_into_parts_at_file_boundaries():
    half = books.MAX_BOOK_BYTES // 2 + 1  # two files never fit in one part
    snap = snapshot(_chapters(size_bytes=half))
    assert set(snap.books) == {"bk:+1", "bk:+2"}
    one, two = snap.books["bk:+1"], snap.books["bk:+2"]
    assert one.title == "Book (part 1/2)" and one.track_no == 1 and two.track_no == 2
    assert one.path.endswith("Book (part 1 of 2).m4b")
    assert [p.ref.item_id for p in books.parts_of(two)] == ["bk:2"]
    assert one.position_s == 60.0 and two.position_s == 0.0 and one.duration_s == 60.0
    assert snap.track("bk:1") is snap.tracks["bk:1"]  # a one-file part is not its file


def test_split_balances_parts():
    sizes = [10, 10, 10, 10, 10, 10]
    parts = [
        track(f"b:{i}", size_bytes=s * books.MAX_BOOK_BYTES // 40) for i, s in enumerate(sizes)
    ]
    groups = books.split(parts)
    assert groups is not None and [len(g) for g in groups] == [3, 3]


def _device(combine: bool = False):
    return DeviceConfig(
        id="d",
        type="fake",
        name="D",
        library_ids=["lib"],
        rule_set=RuleSet(rules=[ALL]),
        sync={"combine_audiobooks": combine},
    )


def test_planner_swaps_the_files_of_a_book_for_the_book_on_merging_devices():
    snap = snapshot(_chapters())
    caps = DeviceCapabilities(formats={"mp3", "aac"}, supports_audiobooks=True)
    plain = plan_sync(_device(), {"lib": snap}, None, caps, Chain([]))
    assert {a.ref.item_id for a in plain.actions} == {"bk:1", "bk:2", "solo:1", "m1"}
    merged = plan_sync(_device(combine=True), {"lib": snap}, None, caps, Chain([]))
    adds = {a.ref.item_id: a for a in merged.actions if a.kind is ActionKind.ADD}
    assert set(adds) == {"bk", "solo:1", "m1"}
    assert "2 files → 1 book (2 chapters)" in adds["bk"].reason


def test_combine_default_per_device_type():
    """a new iPod combines books, other players don't; a stored config keeps its choice, an old one
    takes the type default or its `merge_audiobooks` setting."""
    new = {"id": "x", "name": "X"}
    assert DeviceConfig(**new, type="ipod").sync.combine_audiobooks
    for t in ("rockbox", "hiby", "flashdrive", "ftp", "smb"):
        assert not DeviceConfig(**new, type=t).sync.combine_audiobooks
    assert not DeviceConfig(
        **new, type="ipod", sync={"combine_audiobooks": False}
    ).sync.combine_audiobooks
    assert DeviceConfig(
        **new, type="rockbox", sync={"merge_audiobooks": True}
    ).sync.combine_audiobooks
    stored = DeviceConfig(**new, type="ipod", sync={"combine_audiobooks": False}).model_dump(
        mode="json"
    )
    assert DeviceConfig.model_validate(stored).sync.combine_audiobooks is False


def test_merge_setting_merges_books_on_folder_players():
    snap = snapshot(_chapters())
    caps = DeviceCapabilities(formats={"mp3", "aac"}, supports_audiobooks=True)
    adds = {
        a.ref.item_id
        for a in plan_sync(_device(combine=True), {"lib": snap}, None, caps, Chain([])).actions
    }
    assert adds == {"bk", "solo:1", "m1"}


def test_plan_names_the_part():
    snap = snapshot(_chapters(size_bytes=books.MAX_BOOK_BYTES // 2 + 1))
    caps = DeviceCapabilities(formats={"mp3", "aac"}, supports_audiobooks=True)
    adds = {
        a.ref.item_id: a
        for a in plan_sync(_device(combine=True), {"lib": snap}, None, caps, Chain([])).actions
    }
    assert set(adds) == {"bk:+1", "bk:+2", "solo:1", "m1"}
    assert "1 file → part 2 of 2 (1 chapter)" in adds["bk:+2"].reason


def _ffprobe(path):
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_chapters",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return json.loads(out)


@pytest.mark.parametrize("ext", ["mp3", "m4a"])
async def test_merge_book_writes_one_aac_m4b_with_a_chapter_per_file(tmp_path, ext):
    files = [
        make_track(tmp_path / f"{i}.{ext}", seconds=s, title=f"Розділ {i}")
        for i, s in ((1, 2.0), (2, 3.0))
    ]
    parts = [
        track(
            f"bk:{i}",
            title=f"Розділ {i}",
            kind=MediaKind.AUDIOBOOK,
            album="Книга",
            album_id="bk",
            chapter_index=i,
        )
        for i in (1, 2)
    ]
    book = snapshot(parts).books["bk"]
    out = tmp_path / "w" / "book.m4b"
    out.parent.mkdir()
    await books.merge_book(book, files, out)
    info = _ffprobe(out)
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert [s["codec_name"] for s in audio] == ["aac"]
    chapters = info["chapters"]
    assert [c["tags"]["title"] for c in chapters] == ["Розділ 1", "Розділ 2"]
    assert float(chapters[1]["start_time"]) == pytest.approx(2.0, abs=0.15)
    assert float(info["format"]["duration"]) == pytest.approx(5.0, abs=0.3)
    assert info["format"]["tags"]["title"] == "Книга"


async def test_merge_book_re_encodes_stereo_parts_as_mono_speech(tmp_path):
    """re-encoding hours of books at 128k stereo took ~35 min per book on the Pi; speech profile."""
    files = []
    for i in (1, 2):
        f = tmp_path / f"{i}.mp3"
        subprocess.run(  # noqa: ASYNC221
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=duration=2",
                "-ac",
                "2",
                "-b:a",
                "192k",
                str(f),
            ],
            check=True,
        )
        files.append(f)
    parts = [
        track(
            f"bk:{i}",
            kind=MediaKind.AUDIOBOOK,
            album="Книга",
            album_id="bk",
            chapter_index=i,
            bitrate_kbps=192,
        )
        for i in (1, 2)
    ]
    book = snapshot(parts).books["bk"]
    assert book.bitrate_kbps == books.MAX_KBPS == 64
    out = tmp_path / "w" / "book.m4b"
    out.parent.mkdir()
    seen: list[float] = []
    await books.merge_book(book, files, out, seen.append)
    assert seen and max(seen) == pytest.approx(4.0, abs=0.3)  # ffmpeg's -progress reached the end
    [audio] = [s for s in _ffprobe(out)["streams"] if s["codec_type"] == "audio"]
    assert audio["codec_name"] == "aac" and audio["channels"] == 1


@pytest.mark.parametrize("always", [True, False])
async def test_merged_book_is_cached_even_without_cache(tmp_path, monkeypatch, always):
    """no-cache mode (cache=False) still keeps the expensive merge; the second prepare reuses it.
    with 'Always cache combined audiobooks' off the merge follows the device's cache mode."""
    from syncmusic.core.models import LibraryConfig
    from syncmusic.core.prepare import Preparer
    from syncmusic.infra.cache import ProcessingCache
    from syncmusic.infra.db import Database
    from syncmusic.libraries.folder import FolderLibrary

    root = tmp_path / "lib"
    for i in (1, 2):
        make_track(
            root / "Books" / "Книга" / f"{i}.mp3", title=f"Розділ {i}", album="Книга", tracknumber=i
        )
    settings = {"root": str(root), "subfolder_kinds": {"Books": "audiobook"}}
    lib = FolderLibrary(LibraryConfig(id="f", type="folder", name="F", settings=settings), None)
    snap = await lib.snapshot()
    merges = []
    real = books.merge_book

    async def counting(book, files, out, on_time=None):
        merges.append(book.ref.item_id)
        return await real(book, files, out, on_time)

    monkeypatch.setattr(books, "merge_book", counting)
    db = Database(tmp_path / "sd.db")
    await db.open()
    try:
        preparer = Preparer(ProcessingCache(db, tmp_path / "cache"), tmp_path / "work")
        preparer.cache_books = always
        [book] = snap.books.values()
        for n in (1, 2):
            work = tmp_path / f"w{n}"
            work.mkdir()
            got = await preparer.original(lib, book, work, cache=False)
            assert isinstance(got, Path) is not always, "a cache entry only when always cached"
        assert len(merges) == (1 if always else 2)
    finally:
        await db.close()


async def test_at_most_two_merges_at_once(tmp_path, monkeypatch):
    """four books prepared at once merge two at a time."""
    from syncmusic.core import prepare
    from syncmusic.infra.cache import ProcessingCache
    from syncmusic.infra.db import Database

    live = peak = 0

    async def slow(book, files, out, on_time):
        nonlocal live, peak
        assert book.ref.key() in preparer.merging
        on_time(1.0)
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.05)
        out.write_bytes(b"m4b")
        live -= 1
        return []

    async def part(library, track, work_dir, cache=True):
        f = work_dir / f"{track.ref.item_id.replace(':', '_')}.mp3"
        f.write_bytes(b"mp3")
        return f

    monkeypatch.setattr(books, "merge_book", slow)
    parts = [
        track(
            f"b{b}:{i}", kind=MediaKind.AUDIOBOOK, album=f"B{b}", album_id=f"b{b}", chapter_index=i
        )
        for b in range(4)
        for i in (1, 2)
    ]
    db = Database(tmp_path / "sd.db")
    await db.open()
    try:
        preparer = prepare.Preparer(ProcessingCache(db, tmp_path / "cache"), tmp_path / "work")
        real = preparer.original

        async def original(library, t, work_dir, cache=True):
            return await (real if books.is_book(t) else part)(library, t, work_dir, cache)

        monkeypatch.setattr(preparer, "original", original)
        dirs = [tmp_path / f"w{n}" for n in range(4)]
        for d in dirs:
            d.mkdir()
        bks = list(snapshot(parts).books.values())
        await asyncio.gather(
            *(preparer.original(None, b, d) for b, d in zip(bks, dirs, strict=True))
        )
        assert peak == prepare.MAX_MERGES == 2 and not preparer.merging
    finally:
        await db.close()


def test_merge_status_line(monkeypatch):
    from syncmusic.core import prepare

    now = [1000.0]
    monkeypatch.setattr(prepare.time, "monotonic", lambda: now[0])
    m = prepare.Merge("Хрещення вогнем", 13 * 3600.0)
    assert m.status() == "Combining “Хрещення вогнем” - 0.0 of 13.0 h"
    now[0] += 300
    m.done_s = 3600.0  # 1 h of the book in 5 min: 12 h left take 60 min
    assert m.status() == "Combining “Хрещення вогнем” - 1.0 of 13.0 h, ~60 min left"
    assert prepare.Merge("Short", 600.0, done_s=0.0).status().endswith("0.0 of 10.0 min")


async def test_plan_warns_about_merges_to_encode(tmp_path):
    """the plan says how many books get re-encoded and roughly for how long; AAC parts (stream copy)
    and books already in the cache don't count."""
    from syncmusic.core import prepare
    from syncmusic.infra.cache import ProcessingCache, make_key
    from syncmusic.infra.db import Database

    def book(b, codec, hours):
        parts = [
            track(
                f"{b}:{i}",
                kind=MediaKind.AUDIOBOOK,
                album=b,
                album_id=b,
                chapter_index=i,
                codec=codec,
                duration_s=hours * 1800,
            )
            for i in (1, 2)
        ]
        return snapshot(parts).books[b]

    db = Database(tmp_path / "sd.db")
    await db.open()
    try:
        preparer = prepare.Preparer(ProcessingCache(db, tmp_path / "cache"), tmp_path / "work")
        mp3, aac, cached = book("a", "mp3", 13), book("b", "aac", 5), book("c", "mp3", 6)
        f = tmp_path / "x.m4b"
        f.write_bytes(b"m4b")
        key = make_key("original", cached.ref.library_id, cached.ref.item_id, cached.source_state)
        await preparer.cache.put(key, {"original.m4b": f}, {"name": "original.m4b"})
        assert await preparer.merge_warning([aac, cached]) is None
        note = await preparer.merge_warning([mp3, aac, cached, track("song")])
        assert note == (
            "combining 1 audiobook (13.0 h of audio) into .m4b: about 13 min of encoding before they are written"
        )
    finally:
        await db.close()


@pytest.fixture
def merge_server(tmp_path, monkeypatch):
    """A book of three real files (2 s, 3 s, 2 s), listened to 4 s (1 s into chapter 2)."""
    abs_ = FakeABS()
    abs_.add_book("b1", "Книга", [("01.mp3", 2.0), ("02.mp3", 3.0), ("03.mp3", 2.0)])
    for i, item in enumerate(abs_.items["b1"]["media"]["tracks"], 1):
        f = make_track(
            tmp_path / "src" / f"b{i}.mp3", seconds=item["duration"], title=f"Розділ {i}"
        )
        abs_.files[("b1", item["ino"])] = f.read_bytes()
        item["metadata"]["size"] = f.stat().st_size
    abs_.set_progress("b1", 4.0)
    monkeypatch.setattr(AudiobookshelfLibrary, "transport", abs_)
    return abs_


@pytest.fixture
def server(merge_server):  # the roundtrip `env` uses `server`
    return merge_server


def _book_on_ipod(mount):
    entry = manifest_io.read(mount).items["abs/b1"]
    db = IPodDB(str(mount))
    return db, entry, next(t for t in db.tracks if str(t.dbid) == entry.device_ref)


async def test_multi_file_book_is_one_ipod_track_with_chapters_and_positions_map(env, server):
    app, c, mount = env
    items = manifest_io.read(mount).items
    assert "abs/b1" in items and not any(k.startswith("abs/b1:") for k in items)
    db, entry, t = _book_on_ipod(mount)
    assert t.bookmark_time == pytest.approx(4000, abs=50)  # ABS book time = merged file time
    info = _ffprobe(mount / entry.path.lstrip("/"))
    assert [ch["tags"]["title"] for ch in info["chapters"]] == ["01", "02", "03"]
    assert float(info["format"]["duration"]) == pytest.approx(7.0, abs=0.4)

    t.bookmark_time = 5_500  # listened on: 2.5 s into chapter 2
    db.save()
    report = await _sync(app, c)
    assert report["reverse"]["positions_pushed"] == 1
    prog = next(p for p in server.progress if p["libraryItemId"] == "b1")
    assert prog["currentTime"] == pytest.approx(5.5) and not prog.get("episodeId")

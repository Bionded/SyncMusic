"""A multi-file audiobook above the FAT32 limit goes onto the iPod as parts with chapters."""

# ruff: noqa: F811 - fixtures imported from test_position_roundtrip

from __future__ import annotations

import pytest
from pygpod import Database as IPodDB

from syncmusic.core import books
from syncmusic.core import manifest as manifest_io
from syncmusic.libraries.audiobookshelf import AudiobookshelfLibrary
from tests.fake_abs import FakeABS
from tests.media import make_track
from tests.unit.test_books import _ffprobe
from tests.unit.test_position_roundtrip import _sync, env  # noqa: F401 - fixture


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A book of three real files (2 s, 3 s, 2 s) with a limit that fits two of them, listened to 4 s."""
    abs_ = FakeABS()
    abs_.add_book("b1", "Книга", [("01.mp3", 2.0), ("02.mp3", 3.0), ("03.mp3", 2.0)])
    sizes = []
    for i, item in enumerate(abs_.items["b1"]["media"]["tracks"], 1):
        f = make_track(
            tmp_path / "src" / f"b{i}.mp3", seconds=item["duration"], title=f"Розділ {i}"
        )
        abs_.files[("b1", item["ino"])] = f.read_bytes()
        item["metadata"]["size"] = f.stat().st_size
        sizes.append(f.stat().st_size)
    monkeypatch.setattr(books, "MAX_BOOK_BYTES", sum(sizes) - min(sizes) // 2)
    abs_.set_progress("b1", 4.0)
    monkeypatch.setattr(AudiobookshelfLibrary, "transport", abs_)
    return abs_


def _parts(mount):
    items = manifest_io.read(mount).items
    return {k: e for k, e in items.items() if k.startswith("abs/b1")}


async def test_split_book_goes_on_as_parts_and_positions_map_both_ways(env, server):
    app, c, mount = env
    parts = _parts(mount)
    assert len(parts) == 2 and all(":+" in k for k in parts)
    db = IPodDB(str(mount))
    by_ref = {str(t.dbid): t for t in db.tracks}
    first, second = sorted(parts.values(), key=lambda e: by_ref[e.device_ref].title)
    chapters = [len(_ffprobe(mount / e.path.lstrip("/"))["chapters"]) for e in (first, second)]
    assert sum(chapters) == 3
    t1, t2 = by_ref[first.device_ref], by_ref[second.device_ref]
    assert "(part 1/2)" in t1.title and "(part 2/2)" in t2.title
    # ABS 4.0 s: in part 1 (2 s or 5 s long) → part 1 at its end or at 4 s, part 2 at 4 − len(part 1)
    len1 = 2.0 if chapters[0] == 1 else 5.0
    assert t1.bookmark_time == pytest.approx(min(4.0, len1) * 1000, abs=60)
    assert t2.bookmark_time == pytest.approx(max(4.0 - len1, 0.0) * 1000, abs=60)

    t2.bookmark_time = t2.bookmark_time + 1000  # listened 1 s more in part 2
    db.save()
    report = await _sync(app, c)
    assert report["reverse"]["positions_pushed"] >= 1
    prog = next(p for p in server.progress if p["libraryItemId"] == "b1")
    assert prog["currentTime"] == pytest.approx(max(5.0, len1 + 1.0), abs=0.1)

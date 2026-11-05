"""Positions of a multi-file audiobook listened across a chapter boundary on the iPod (follow-up).

Unlike FakeABS's default fixed 2023 clock, these tests stamp progress like the real server does: an update made by
the sync itself gets the current time as its `lastUpdate`."""

# ruff: noqa: F811 - fixtures imported from test_position_roundtrip

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from pygpod import Database as IPodDB

from syncmusic.core import manifest as manifest_io
from syncmusic.libraries.audiobookshelf import AudiobookshelfLibrary
from tests.fake_abs import FakeABS
from tests.media import make_track
from tests.unit.test_position_roundtrip import (  # noqa: F401 - fixtures
    _book_position,
    _sync,
    chapter_files,
    env,
)


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A book of three 10-minute files, listened to 700 s (100 s into chapter 2) two days ago on another client."""
    abs_ = FakeABS()
    abs_.clock = int((time.time() - 2 * 86400) * 1000)
    abs_.add_book("b1", "Книга", [("01.mp3", 600.0), ("02.mp3", 600.0), ("03.mp3", 600.0)])
    for i, item in enumerate(abs_.items["b1"]["media"]["tracks"], 1):
        chapter = make_track(tmp_path / "src" / f"b{i}.mp3", seconds=2.0, title=f"Розділ {i}")
        abs_.files[("b1", item["ino"])] = chapter.read_bytes()
        item["metadata"]["size"] = chapter.stat().st_size
    abs_.set_progress("b1", 700.0)
    monkeypatch.setattr(AudiobookshelfLibrary, "transport", abs_)
    return abs_


def _chapter(mount, n):
    dbid = manifest_io.read(mount).items[f"abs/b1:b1-{n}"].device_ref
    db = IPodDB(str(mount))
    return db, next(t for t in db.tracks if str(t.dbid) == dbid)


def _listened(mount, n, position_s, minutes_ago):
    db, t = _chapter(mount, n)
    t.bookmark_time = int(position_s * 1000)
    # the iPod keeps wall-clock time in its zone; pygpod reads and writes it labelled as UTC
    t.time_played = (datetime.now() - timedelta(minutes=minutes_ago)).replace(tzinfo=UTC)
    db.save()


async def test_listening_across_a_chapter_boundary_keeps_the_later_chapter(env, server):
    """On the iPod: chapter 2 to 590 s (30 min ago), then on into chapter 3 to 100 s (20 min ago). Nothing happened on
    the phone. ABS must end at 1200 + 100 s and the iPod keep chapter 3 at 100 s: the sync's own push of one chapter
    must not make the library look 'newer' than the device's other chapter."""
    app, c, mount = env
    _listened(mount, 2, 590.0, 30)
    _listened(mount, 3, 100.0, 20)
    server.clock = int(time.time() * 1000)  # the sync's PATCHes are stamped "now", like ABS does
    report = await _sync(app, c)
    assert _book_position(server) == pytest.approx(1300.0), (report["reverse"], server.patches)
    _, t3 = _chapter(mount, 3)
    assert t3.bookmark_time == 100_000


async def test_one_chapter_moved_on_the_device_is_pushed_without_a_conflict(env, server, caplog):
    """Only chapter 2 moved on the iPod (to 400 s): pushed as 1000 s, no position conflict (the phone did not move)."""
    import logging

    app, c, mount = env
    _listened(mount, 2, 400.0, 20)
    server.clock = int(time.time() * 1000)
    with caplog.at_level(logging.INFO, logger="syncmusic.core.reverse"):
        report = await _sync(app, c)
    assert report["reverse"]["positions_pushed"] == 1
    assert _book_position(server) == pytest.approx(1000.0)
    assert "position conflict" not in caplog.text


async def test_the_phone_newer_than_the_ipod_wins_for_the_whole_book(env, server, caplog):
    """The iPod moved chapters 2 and 3 (30 and 20 min ago), the phone moved the book to 1500 s 10 min ago: the
    library wins for the book, nothing is sent, and the forward sync puts the phone's position on both chapters."""
    import logging

    app, c, mount = env
    _listened(mount, 2, 590.0, 30)
    _listened(mount, 3, 100.0, 20)
    server.clock = int((time.time() - 600) * 1000) - 1000
    server.set_progress("b1", 1500.0)
    server.clock = int(time.time() * 1000)
    with caplog.at_level(logging.INFO, logger="syncmusic.core.reverse"):
        report = await _sync(app, c)
    assert report["reverse"]["positions_pushed"] == 0
    assert report["reverse"]["positions_superseded"] == 2
    assert not server.patches
    assert _book_position(server) == pytest.approx(1500.0)
    assert caplog.text.count("position conflict") == 1  # one book, one decision
    _, t2 = _chapter(mount, 2)
    _, t3 = _chapter(mount, 3)
    assert (t2.bookmark_time, t3.bookmark_time) == (600_000, 300_000)


async def test_the_newest_chapter_decides_whatever_the_row_order():
    """Chapter 3's row is delivered before chapter 2's (an older pending row first): chapter 3, the newer one on
    the device, is the book's position; chapter 2 follows it."""
    from syncmusic.core.reverse import _covered_positions
    from syncmusic.infra.store import PendingRow

    class Sink:
        async def position_scope(self, ref):
            return "abs/b1" if ref.item_id.startswith("b1:") else ref.key()

    now = datetime.now(UTC)

    def row(i, n, minutes_ago):
        at = (now - timedelta(minutes=minutes_ago)).isoformat()
        return PendingRow(i, "position", {"ref": f"abs/b1:b1-{n}", "updated_at": at}, 0, None)

    rows = [
        row(1, 3, 20),
        row(2, 2, 30),
        PendingRow(3, "position", {"ref": "abs/p1:e1", "updated_at": None}, 0, None),
    ]
    covered = await _covered_positions(Sink(), rows)  # type: ignore[arg-type]
    assert {k: v.id for k, v in covered.items()} == {2: 1}


async def _phone_in_chapter_3_ipod_rewinds_chapter_2(app, c, mount, server, phone_minutes_ago):
    """Last sync: the book at 1500 s (chapter 3 at 300 s) on both sides. Then the iPod rewinds chapter 2 to 200 s
    (20 min ago) and the phone listens on in chapter 3 to 1700 s (`phone_minutes_ago`). Chapter 2's position in
    the library stays clamped at its end (600 s), so only the book's position shows the phone's move."""
    server.set_progress("b1", 1500.0)
    await _sync(app, c)
    _, t3 = _chapter(mount, 3)
    assert t3.bookmark_time == 300_000
    _listened(mount, 2, 200.0, 20)
    server.clock = int((time.time() - phone_minutes_ago * 60) * 1000)
    server.set_progress("b1", 1700.0)
    server.clock = int(time.time() * 1000)
    return len(server.patches)


async def test_a_newer_phone_move_in_another_chapter_wins(env, server, caplog):
    """The phone's newer move in chapter 3 is seen although chapter 2's library
    position did not change: the library wins, nothing is sent, the iPod gets the phone's position; the next sync
    changes nothing (no ping-pong)."""
    import logging

    app, c, mount = env
    patches = await _phone_in_chapter_3_ipod_rewinds_chapter_2(app, c, mount, server, 10)
    with caplog.at_level(logging.INFO, logger="syncmusic.core.reverse"):
        report = await _sync(app, c)
    assert report["reverse"]["positions_pushed"] == 0
    assert report["reverse"]["positions_superseded"] == 1
    assert len(server.patches) == patches
    assert _book_position(server) == pytest.approx(1700.0)
    assert "library wins" in caplog.text
    _, t2 = _chapter(mount, 2)
    _, t3 = _chapter(mount, 3)
    assert (t2.bookmark_time, t3.bookmark_time) == (600_000, 500_000)

    again = await _sync(app, c)
    assert again["reverse"]["positions_pushed"] == again["reverse"]["positions_superseded"] == 0
    assert len(server.patches) == patches and _book_position(server) == pytest.approx(1700.0)


async def test_an_older_phone_move_in_another_chapter_loses_to_the_ipod(env, server):
    """The same, but the phone moved 30 min ago, before the iPod's rewind: the iPod's chapter 2 is the book's
    position (800 s), and the next sync changes nothing."""
    app, c, mount = env
    await _phone_in_chapter_3_ipod_rewinds_chapter_2(app, c, mount, server, 30)
    report = await _sync(app, c)
    assert report["reverse"]["positions_pushed"] == 1
    assert _book_position(server) == pytest.approx(800.0)
    patches = len(server.patches)

    again = await _sync(app, c)
    assert again["reverse"]["positions_pushed"] == again["reverse"]["positions_superseded"] == 0
    assert len(server.patches) == patches and _book_position(server) == pytest.approx(800.0)

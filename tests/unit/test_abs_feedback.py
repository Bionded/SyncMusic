"""Audiobookshelf FeedbackSink: progress PATCH bodies."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from syncmusic.base.feedback import FeedbackRejected
from syncmusic.core.models import ItemRef, PlayEvent
from tests.contracts.harness import _abs_library


@pytest.fixture
def lib(tmp_path):
    lib = _abs_library(tmp_path)  # book b1: chapters 60 s + 30 s; podcast p1: episode e1 of 100 s
    lib.transport.add_book("b3", "Три", [("1.mp3", 100.0), ("2.mp3", 100.0), ("3.mp3", 100.0)])
    return lib


def _ref(item: str) -> ItemRef:
    return ItemRef(library_id="abs1", item_id=item)


def _stored(lib, item: str, episode: str | None = None) -> dict:
    return next(
        p for p in lib.transport.progress if (p["libraryItemId"], p["episodeId"]) == (item, episode)
    )


async def test_book_chapter_position_is_book_current_time(lib):
    await lib.set_position(_ref("b3:b3-2"), 25.0, False)
    assert lib.transport.patches[-1] == (
        "b3",
        None,
        {"currentTime": 125.0, "duration": 300.0, "progress": 125.0 / 300.0, "isFinished": False},
    )
    t = (await lib.snapshot()).tracks["b3:b3-2"]
    assert t.position_s == pytest.approx(25.0) and not t.finished


async def test_finished_chapter_is_its_end_not_a_finished_book(lib):
    await lib.set_position(_ref("b3:b3-1"), 3.0, True)
    _, _, body = lib.transport.patches[-1]
    assert body["currentTime"] == 100.0 and body["isFinished"] is False
    # the last chapter played -> the book is finished
    await lib.set_position(_ref("b3:b3-3"), 0.0, True)
    assert lib.transport.patches[-2][2] == {
        "currentTime": 300.0,
        "duration": 300.0,
        "progress": 1.0,
        "isFinished": True,
    }
    # ABS un-finishes an entry whose position moved: the flag is sent again on its own
    assert lib.transport.patches[-1][2] == {"isFinished": True, "progress": 1.0}
    assert _stored(lib, "b3")["isFinished"] and _stored(lib, "b3")["currentTime"] == 300.0


async def test_within_30_s_of_the_end_is_finished(lib):
    await lib.set_position(_ref("b3:b3-3"), 69.0, False)  # 269 of 300: not yet
    assert _stored(lib, "b3")["isFinished"] is False
    await lib.set_position(_ref("b3:b3-3"), 71.0, False)  # 271 >= 300 - 30
    assert _stored(lib, "b3")["isFinished"] is True and _stored(lib, "b3")["currentTime"] == 271.0


async def test_episode_progress(lib):
    await lib.set_position(_ref("p1:e1"), 40.0, False)
    assert lib.transport.patches[-1] == (
        "p1",
        "e1",
        {"currentTime": 40.0, "duration": 100.0, "progress": 0.4, "isFinished": False},
    )
    await lib.set_position(_ref("p1:e1"), 10.0, True)  # the device marked it played
    assert _stored(lib, "p1", "e1")["isFinished"] is True
    assert (await lib.snapshot()).tracks["p1:e1"].finished


async def test_get_position_in_the_items_own_time_with_its_update_time(lib):
    """what reverse sync compares with a device position (a chapter: relative to its start)."""
    assert await lib.get_position(_ref("b3:b3-2")) is None  # no progress yet
    lib.transport.set_progress("b3", 125.0)
    pos = await lib.get_position(_ref("b3:b3-2"))
    assert pos.position_s == pytest.approx(25.0) and not pos.finished
    assert pos.updated_at == datetime.fromtimestamp(lib.transport.clock / 1000, UTC)
    lib.transport.set_progress("p1", 40.0, episode="e1")
    assert (await lib.get_position(_ref("p1:e1"))).position_s == pytest.approx(40.0)
    assert await lib.get_position(_ref("nope:x")) is None  # gone: set_position reports it


async def test_unknown_items_raise(lib):
    with pytest.raises(FeedbackRejected):  # 404
        await lib.set_position(_ref("nope:x"), 1.0, False)
    with pytest.raises(FeedbackRejected):
        await lib.set_position(_ref("b3:no-such-ino"), 1.0, False)
    with pytest.raises(FeedbackRejected):
        await lib.set_position(ItemRef(library_id="other", item_id="b3:b3-1"), 1.0, False)
    assert lib.transport.patches == []


async def test_plays_accepted_not_stored_ratings_noop(lib):
    ts = datetime(2026, 10, 1, tzinfo=UTC)
    own = PlayEvent(ref=_ref("p1:e1"), played_at=ts)
    foreign = PlayEvent(ref=ItemRef(library_id="x", item_id="p1:e1"), played_at=ts)
    res = await lib.submit_plays([own, foreign])
    assert res.accepted == [own] and [p for p, _ in res.rejected] == [foreign] and res.failed == []
    await lib.set_rating(_ref("p1:e1"), 5)
    assert lib.transport.patches == []


async def test_scope_positions_are_whole_books_and_episodes(lib):
    """one entry per book (its book time) and per episode, keyed like `position_scope`."""
    await lib.set_position(_ref("b3:b3-2"), 25.0, False)
    await lib.set_position(_ref("p1:e1"), 40.0, False)
    scopes = await lib.scope_positions()
    assert scopes["abs1/b3"].position_s == pytest.approx(125.0)
    assert scopes["abs1/p1:e1"].position_s == pytest.approx(40.0)
    assert await lib.position_scope(_ref("b3:b3-2")) == "abs1/b3"
    assert await lib.position_scope(_ref("p1:e1")) == "abs1/p1:e1"

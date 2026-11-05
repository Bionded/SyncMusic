"""`ReverseSync._library_newer` branch by branch: the 1 s "did not move" threshold, the scope vs item
position, unknown times and the strict "library newer" comparison (mutation survivors)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from syncmusic.core.models import ItemRef, PositionInfo
from syncmusic.core.reverse import ReverseSync, _Outcome

REF = ItemRef(library_id="abs", item_id="b1")
DEVICE_AT = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
EARLIER = datetime(2024, 1, 1, 11, 0, tzinfo=UTC)
LATER = datetime(2024, 1, 1, 13, 0, tzinfo=UTC)


class _Sink:
    def __init__(self, item: PositionInfo | None, scope: str | None = None) -> None:
        self.item, self.scope = item, scope
        self.item_calls = 0

    async def position_scope(self, ref: ItemRef) -> str | None:
        return self.scope

    async def get_position(self, ref: ItemRef) -> PositionInfo | None:
        self.item_calls += 1
        return self.item


def _data(**kw: Any) -> dict[str, Any]:
    return {"position_s": 50.0, "updated_at": DEVICE_AT.isoformat(), **kw}


async def _newer(sink: _Sink, data: dict[str, Any], out: _Outcome | None = None) -> bool:
    return await ReverseSync(store=None)._library_newer(sink, REF, data, out or _Outcome())  # type: ignore[arg-type]


async def test_no_device_time_keeps_the_device_position():
    sink = _Sink(PositionInfo(position_s=90, updated_at=LATER))
    assert await _newer(sink, _data(updated_at=None)) is False
    assert sink.item_calls == 0


@pytest.mark.parametrize(
    ("lib_at", "expected"), [(LATER, True), (EARLIER, False), (DEVICE_AT, False)]
)
async def test_item_position_newer_strictly(lib_at, expected):
    sink = _Sink(PositionInfo(position_s=90, updated_at=lib_at))
    assert await _newer(sink, _data(last_s=10.0)) is expected


@pytest.mark.parametrize(
    ("lib_pos", "expected"), [(10.9, False), (9.1, False), (11.0, True), (9.0, True)]
)
async def test_item_did_not_move_within_one_second(lib_pos, expected):
    sink = _Sink(PositionInfo(position_s=lib_pos, updated_at=LATER))
    assert await _newer(sink, _data(last_s=10.0)) is expected


async def test_unknown_last_position_compares_times():
    sink = _Sink(PositionInfo(position_s=10.0, updated_at=LATER))
    assert await _newer(sink, _data()) is True


async def test_unknown_library_position_or_time():
    assert await _newer(_Sink(None), _data(last_s=10.0)) is False
    assert await _newer(_Sink(PositionInfo(position_s=90)), _data(last_s=10.0)) is False


def _scoped(now_pos: float, known: float, at: datetime = LATER) -> _Outcome:
    out = _Outcome()
    out.scopes_now = {"abs": {"book": PositionInfo(position_s=now_pos, updated_at=at)}}
    out.known_scopes = {"book": known}
    return out


@pytest.mark.parametrize(
    ("now_pos", "expected"), [(100.5, False), (99.5, False), (101.0, True), (99.0, True)]
)
async def test_scope_did_not_move_within_one_second(now_pos, expected):
    sink = _Sink(PositionInfo(position_s=10.0, updated_at=EARLIER), scope="book")
    # the item's own position would say "did not move" (last_s) and "older": only the scope decides
    assert await _newer(sink, _data(last_s=10.0), _scoped(now_pos, 100.0)) is expected
    assert sink.item_calls == 0


async def test_scope_moved_but_older_than_the_device():
    sink = _Sink(None, scope="book")
    assert await _newer(sink, _data(), _scoped(200.0, 100.0, EARLIER)) is False


@pytest.mark.parametrize("missing", ["now", "known", "other_library"])
async def test_scope_unknown_falls_back_to_the_item(missing):
    out = _scoped(200.0, 100.0)
    if missing == "now":
        out.scopes_now["abs"] = {}
    elif missing == "known":
        out.known_scopes = {}
    else:
        out.scopes_now = {"nav": out.scopes_now["abs"]}
    sink = _Sink(PositionInfo(position_s=10.0, updated_at=LATER), scope="book")
    assert await _newer(sink, _data(last_s=10.0), out) is False  # item did not move
    assert sink.item_calls == 1

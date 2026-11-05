"""`ReverseSync._deliver_one` and `_send_play` row by row: which store call each kind makes with and without a
capable sink, the report counters and what reaches `on_delivered` (mutation survivors)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from syncmusic.base.capabilities import LibraryCapabilities
from syncmusic.base.feedback import FeedbackRejected
from syncmusic.core.models import ItemRef, PlayEvent, PositionInfo
from syncmusic.core.reverse import ReverseSync, _Outcome
from syncmusic.infra.store import PendingRow

REF = ItemRef(library_id="lib", item_id="t1")
AT = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
ALL = LibraryCapabilities(
    supports_scrobble=True, supports_ratings=True, supports_starred=True, supports_positions=True
)
NONE = LibraryCapabilities()


class _Store:
    def __init__(self, submitted: bool = False) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.submitted = submitted

    async def delivered(self, row_id: int, sink: str | None = None, plays: Any = None) -> None:
        self.calls.append(("delivered", row_id, sink, len(plays) if plays else None))

    async def delete_pending(self, row_ids: list[int]) -> None:
        self.calls.append(("delete", row_ids))

    async def add_local_feedback(self, library_id: str, item_id: str, **kw: Any) -> None:
        self.calls.append(("local", library_id, item_id, kw))

    async def submitted_plays(self, name: str, plays: list[PlayEvent]) -> bool:
        self.calls.append(("submitted?", name))
        assert [p.ref for p in plays] == [REF]
        return self.submitted

    async def set_inflight(self, row_id: int, count: int | None) -> None:
        self.calls.append(("inflight", row_id, count))


class _Sink:
    sink_name = "nd"

    def __init__(self, result: Any = None, counts: list[int | None] | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.result = result or SimpleNamespace(accepted=[1], rejected=[], failed=[])
        self.counts = counts or [5]

    async def set_rating(self, ref: ItemRef, rating: int) -> None:
        self.calls.append(("rating", ref.key(), rating))

    async def set_starred(self, ref: ItemRef, starred: bool) -> None:
        self.calls.append(("star", ref.key(), starred))

    async def set_position(self, ref: ItemRef, pos: float, finished: bool) -> None:
        self.calls.append(("position", ref.key(), pos, finished))

    async def get_position(self, ref: ItemRef) -> PositionInfo | None:
        return None

    async def play_count(self, ref: ItemRef) -> int | None:
        assert ref == REF
        return self.counts.pop(0)

    async def submit_plays(self, plays: list[PlayEvent]) -> Any:
        self.calls.append(("submit", len(plays)))
        return self.result


def _row(kind: str, data: dict[str, Any], inflight: int | None = None) -> PendingRow:
    return PendingRow(id=7, kind=kind, data=data, attempts=0, last_error=None, inflight=inflight)


def _play() -> PendingRow:
    return _row("play", PlayEvent(ref=REF, played_at=AT).model_dump(mode="json"))


async def _run(
    store: _Store,
    sink: _Sink | None,
    caps: LibraryCapabilities,
    r: PendingRow,
    out: _Outcome | None = None,
):
    rs = ReverseSync(store=store)  # type: ignore[arg-type]
    took: list[tuple[str, str]] = []

    async def on_delivered(kind: str, ref: ItemRef, data: dict[str, Any], key: str) -> None:
        assert data is r.data and key == ref.library_id
        took.append((kind, ref.key()))

    rs.on_delivered = on_delivered
    out = out or _Outcome()
    await rs._deliver_one("lib", sink, caps, r, out)  # type: ignore[arg-type]
    return out, took


async def test_rating_to_sink():
    store, sink = _Store(), _Sink()
    out, took = await _run(store, sink, ALL, _row("rating", {"ref": REF.key(), "rating": 4}))
    assert sink.calls == [("rating", "lib/t1", 4)]
    assert store.calls == [("delivered", 7, None, None)]
    assert out.report.ratings_pushed == 1
    assert out.report.consumed == ["lib/t1"]
    assert took == [("rating", "lib/t1")]


@pytest.mark.parametrize("sink", [None, _Sink()])
async def test_rating_kept_locally(sink):
    store = _Store()
    out, took = await _run(store, sink, NONE, _row("rating", {"ref": REF.key(), "rating": 4}))
    assert store.calls == [("local", "lib", "t1", {"rating": 4, "set_rating": True, "done_row": 7})]
    assert out.report.ratings_pushed == 0 and took == [] and out.report.consumed == []


@pytest.mark.parametrize("starred", [True, False])
async def test_star_to_sink(starred):
    store, sink = _Store(), _Sink()
    out, took = await _run(store, sink, ALL, _row("star", {"ref": REF.key(), "starred": starred}))
    assert sink.calls == [("star", "lib/t1", starred)]
    assert store.calls == [("delivered", 7, None, None)]
    assert out.report.stars_pushed == 1 and took == [("star", "lib/t1")]


async def test_star_without_support_is_dropped():
    store, sink = _Store(), _Sink()
    out, took = await _run(store, sink, NONE, _row("star", {"ref": REF.key(), "starred": True}))
    assert sink.calls == [] and store.calls == [("delivered", 7, None, None)]
    assert out.report.stars_pushed == 0 and took == []


@pytest.mark.parametrize("finished", [True, False])
async def test_position_to_sink(finished):
    store, sink = _Store(), _Sink()
    data = {"ref": REF.key(), "position_s": "12.5", "finished": finished}
    out, took = await _run(store, sink, ALL, _row("position", data))
    assert sink.calls == [("position", "lib/t1", 12.5, finished)]
    assert store.calls == [("delivered", 7, None, None)]
    assert out.report.positions_pushed == 1
    assert out.report.finished_pushed == int(finished)
    assert took == [("position", "lib/t1")]
    assert out.report.consumed == (
        ["lib/t1"] if finished else []
    )  # only a finished position is consumed


async def test_position_superseded_by_newer_library():
    store, sink = _Store(), _Sink()

    async def newer(*a: Any) -> PositionInfo:
        return PositionInfo(position_s=99, updated_at=datetime(2025, 1, 1, tzinfo=UTC))

    sink.get_position = newer  # type: ignore[method-assign]
    data = {"ref": REF.key(), "position_s": 12, "updated_at": AT.isoformat(), "last_s": 1}
    out, took = await _run(store, sink, ALL, _row("position", data))
    assert sink.calls == [] and store.calls == [("delete", [7])]
    assert out.superseded == {"lib/t1"} and out.report.positions_superseded == 1
    assert out.report.positions_pushed == 0 and took == []


async def test_position_kept_locally():
    store = _Store()
    out, _ = await _run(store, None, ALL, _row("position", {"ref": REF.key(), "position_s": 3}))
    assert store.calls == [("local", "lib", "t1", {"position_s": 3.0, "done_row": 7})]
    assert out.report.positions_pushed == 0


async def test_play_kept_locally():
    store = _Store()
    out, took = await _run(store, _Sink(), NONE, _play())
    assert store.calls == [("local", "lib", "t1", {"plays": 1, "last_played": AT, "done_row": 7})]
    assert took == [] and out.report.plays_submitted == 0


async def test_play_submitted():
    store, sink = _Store(), _Sink(counts=[5])
    out, took = await _run(store, sink, ALL, _play())
    assert store.calls == [
        ("submitted?", "nd:lib"),
        ("inflight", 7, 5),
        ("delivered", 7, "nd:lib", 1),
    ]
    assert sink.calls == [("submit", 1)]
    assert out.report.plays_submitted == 1 and took == [("play", "lib/t1")]
    assert out.report.consumed == ["lib/t1"]


async def test_play_duplicate_is_not_sent():
    store, sink = _Store(submitted=True), _Sink()
    out, took = await _run(store, sink, ALL, _play())
    assert store.calls == [("submitted?", "nd:lib"), ("delivered", 7, None, None)]
    assert sink.calls == [] and took == []
    assert out.report.plays_duplicate == 1 and out.report.plays_submitted == 0


async def test_inflight_play_that_arrived_is_not_resent():
    store, sink = _Store(), _Sink(counts=[6])
    out, took = await _run(store, sink, ALL, _row("play", _play().data, inflight=5))
    assert store.calls == [("submitted?", "nd:lib"), ("delivered", 7, "nd:lib", 1)]
    assert sink.calls == [] and out.report.plays_submitted == 1 and took == [("play", "lib/t1")]


@pytest.mark.parametrize("now", [5, None])
async def test_inflight_play_that_did_not_arrive_is_resent(now):
    store, sink = _Store(), _Sink(counts=[now, 5])
    out, _ = await _run(store, sink, ALL, _row("play", _play().data, inflight=5))
    assert sink.calls == [("submit", 1)]
    assert ("inflight", 7, 5) in store.calls and out.report.plays_submitted == 1


async def test_play_rejected_and_failed():
    rejected = _Sink(result=SimpleNamespace(accepted=[], rejected=[(None, "bad id")], failed=[]))
    with pytest.raises(FeedbackRejected, match="bad id"):
        await _run(_Store(), rejected, ALL, _play())
    failed = _Sink(result=SimpleNamespace(accepted=[], rejected=[], failed=[(None, "timeout")]))
    with pytest.raises(Exception, match="timeout") as e:
        await _run(_Store(), failed, ALL, _play())
    assert not isinstance(e.value, FeedbackRejected)
    empty = _Sink(result=SimpleNamespace(accepted=[], rejected=[], failed=[]))
    with pytest.raises(Exception, match="^not accepted by the sink$"):
        await _run(_Store(), empty, ALL, _play())


async def test_counters_add_up():
    out, sink = _Outcome(), _Sink(counts=[5, 5, 6, 6])
    for _ in range(2):
        await _run(_Store(), sink, ALL, _row("rating", {"ref": REF.key(), "rating": 4}), out)
        await _run(_Store(), sink, ALL, _row("star", {"ref": REF.key(), "starred": True}), out)
        data = {"ref": REF.key(), "position_s": 1, "finished": True}
        await _run(_Store(), sink, ALL, _row("position", data), out)
        await _run(_Store(), sink, ALL, _play(), out)
        await _run(_Store(submitted=True), sink, ALL, _play(), out)
    for _ in range(2):
        await _run(_Store(), sink, ALL, _row("play", _play().data, inflight=5), out)

    async def newer(*a: Any) -> PositionInfo:
        return PositionInfo(position_s=99, updated_at=datetime(2025, 1, 1, tzinfo=UTC))

    sink.get_position = newer  # type: ignore[method-assign]
    data = {"ref": REF.key(), "position_s": 12, "updated_at": AT.isoformat(), "last_s": 1}
    for _ in range(2):
        await _run(_Store(), sink, ALL, _row("position", data), out)
    r = out.report
    assert (r.ratings_pushed, r.stars_pushed, r.positions_pushed, r.finished_pushed) == (2, 2, 2, 2)
    assert (r.plays_submitted, r.plays_duplicate, r.positions_superseded) == (4, 2, 2)

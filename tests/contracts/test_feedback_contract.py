"""FeedbackSink contract: what every sink of listening data must do.

Parametrised over `harness.FEEDBACK`; every registered library type that is a FeedbackSink needs an entry. What a
sink stored is read back through the library's own snapshot (play count, rating, position).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from syncmusic.base.feedback import FeedbackRejected, FeedbackSink, SubmitResult
from syncmusic.core import registry
from syncmusic.core.models import ItemRef, PlayEvent
from tests.contracts import harness


def test_every_registered_sink_has_a_feedback_harness():
    sinks = {n for n in registry.LIBRARIES if issubclass(registry.library_class(n), FeedbackSink)}
    assert sinks <= set(harness.FEEDBACK)


@pytest.fixture(params=sorted(harness.FEEDBACK))
def sink_case(request):
    return request.param, harness.FEEDBACK[request.param]


def _plays(ref: ItemRef, n: int) -> list[PlayEvent]:
    start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    return [
        PlayEvent(ref=ref, played_at=start + timedelta(minutes=5 * i), source_device="dev")
        for i in range(n)
    ]


async def test_is_a_sink(sink_case, tmp_path):
    name, h = sink_case
    lib = h.make(tmp_path)
    assert isinstance(lib, FeedbackSink) and lib.type_name == name and lib.sink_name
    await lib.close()


async def test_plays_counted_exactly_and_failures_reported(sink_case, tmp_path):
    _, h = sink_case
    lib = h.make(tmp_path)
    snap = await lib.snapshot()
    item_id, track = next(iter(snap.tracks.items()))
    good = _plays(track.ref, 3)
    bad = [
        *_plays(ItemRef(library_id=lib.id, item_id="no-such-item"), 1),
        *_plays(ItemRef(library_id="other-library", item_id=item_id), 1),
    ]
    result = await lib.submit_plays(good + bad)  # type: ignore[attr-defined]
    assert isinstance(result, SubmitResult)
    everything = result.accepted + [p for p, _ in result.failed + result.rejected]
    assert sorted(map(id, everything)) == sorted(map(id, good + bad))  # every play exactly once
    # an item the sink does not have / not of this library can never take plays: rejected (permanent)
    assert result.failed == [] and [p for p, _ in result.rejected] == [
        p for p in bad if p not in result.accepted
    ]
    assert all(msg for _, msg in result.rejected)
    if lib.capabilities.supports_scrobble:
        assert result.accepted == good
        assert [p for p, _ in result.rejected] == bad
        after = (await lib.snapshot()).tracks[item_id]
        assert after.play_count == track.play_count + 3
        # play_count: None = cannot tell, else the server's count after the plays
        count = await lib.play_count(track.ref)  # type: ignore[attr-defined]
        assert count is None or count == after.play_count
        assert await lib.play_count(bad[0].ref) is None  # type: ignore[attr-defined]
    await lib.close()


async def test_rating_round_trip(sink_case, tmp_path):
    _, h = sink_case
    lib = h.make(tmp_path)
    if not lib.capabilities.supports_ratings:
        pytest.skip("sink does not store ratings")
    snap = await lib.snapshot()
    item_id, track = next(iter(snap.tracks.items()))
    await lib.set_rating(track.ref, 4)  # type: ignore[attr-defined]
    assert (await lib.snapshot()).tracks[item_id].rating == 4
    await lib.set_rating(track.ref, None)  # type: ignore[attr-defined]
    assert (await lib.snapshot()).tracks[item_id].rating is None
    with pytest.raises(FeedbackRejected):  # permanent: never silent, never retried forever
        await lib.set_rating(ItemRef(library_id=lib.id, item_id="no-such-item"), 3)  # type: ignore[attr-defined]
    await lib.close()


async def test_position_round_trip_or_noop(sink_case, tmp_path):
    _, h = sink_case
    lib = h.make(tmp_path)
    snap = await lib.snapshot()
    item_id, track = next(iter(snap.tracks.items()))
    await lib.set_position(track.ref, 42.0, False)  # type: ignore[attr-defined]
    after = (await lib.snapshot()).tracks[item_id]
    if lib.capabilities.supports_positions:
        assert after.position_s == pytest.approx(42.0, abs=1) and not after.finished
    else:
        assert after.position_s == track.position_s  # a no-op, not an error
    await lib.close()


async def test_server_down_plays_failed_not_raised(sink_case, tmp_path):
    _, h = sink_case
    lib = h.down(tmp_path)
    ref = ItemRef(library_id=lib.id, item_id="s1")
    plays = _plays(ref, 2)
    result = await lib.submit_plays(plays)  # type: ignore[attr-defined]
    if lib.capabilities.supports_scrobble:  # transient: failed, never rejected
        assert result.accepted == [] and [p for p, _ in result.failed] == plays
    if lib.capabilities.supports_ratings:
        with pytest.raises(Exception) as ei:  # noqa: B017 - a failed push must be visible to the caller
            await lib.set_rating(ref, 3)  # type: ignore[attr-defined]
        assert not isinstance(ei.value, FeedbackRejected)
    if lib.capabilities.supports_positions:
        with pytest.raises(Exception) as ei:  # noqa: B017
            await lib.set_position(ref, 10.0, False)  # type: ignore[attr-defined]
        assert not isinstance(ei.value, FeedbackRejected)
    await lib.close()

"""Navidrome FeedbackSink: the exact Subsonic calls."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from syncmusic.base.feedback import FeedbackRejected
from syncmusic.core.models import ItemRef, PlayEvent
from tests.contracts.harness import _navidrome_library


@pytest.fixture
def lib(tmp_path):
    return _navidrome_library(tmp_path)


def _play(item: str, ts: datetime, lib_id: str = "nd1") -> PlayEvent:
    return PlayEvent(ref=ItemRef(library_id=lib_id, item_id=item), played_at=ts)


async def test_scrobble_time_in_ms_and_submission(lib):
    server = lib.transport
    ts = datetime(2026, 10, 1, 12, 0, 0, 250_000, tzinfo=UTC)
    res = await lib.submit_plays([_play("s1", ts), _play("s1", ts.replace(minute=5))])
    assert len(res.accepted) == 2 and res.failed == []
    ms = int(ts.timestamp()) * 1000 + 250
    assert server.scrobbles == [("s1", ms), ("s1", ms + 300_000)]
    assert server.songs["s1"]["playCount"] == 2
    assert server.calls.count("getSong") == 1  # looked up once per song, not per play


async def test_unknown_song_rejected_not_dropped(lib):
    ts = datetime(2026, 10, 1, tzinfo=UTC)
    res = await lib.submit_plays([_play("gone", ts), _play("s2", ts)])
    assert [p.ref.item_id for p in res.accepted] == ["s2"]
    assert [(p.ref.item_id, "not found" in msg) for p, msg in res.rejected] == [("gone", True)]
    assert res.failed == []
    assert "scrobble" in lib.transport.calls and lib.transport.scrobbles == [
        ("s2", int(ts.timestamp()) * 1000)
    ]


async def test_http_error_reported_per_play(lib):
    class Flaky(httpx.AsyncBaseTransport):
        def __init__(self, inner):
            self.inner, self.n = inner, 0

        async def handle_async_request(self, request):
            if request.url.path.endswith("/scrobble"):
                self.n += 1
                if self.n == 1:
                    return httpx.Response(503)
            return await self.inner.handle_async_request(request)

    lib.transport = Flaky(lib.transport)
    ts = datetime(2026, 10, 1, tzinfo=UTC)
    res = await lib.submit_plays([_play("s1", ts), _play("s2", ts)])
    assert [p.ref.item_id for p in res.accepted] == ["s2"]
    assert res.failed[0][0].ref.item_id == "s1" and "503" in res.failed[0][1]  # transient
    assert res.rejected == []


async def test_rating_values(lib):
    server = lib.transport
    ref = ItemRef(library_id="nd1", item_id="s1")
    await lib.set_rating(ref, 5)
    assert server.songs["s1"]["userRating"] == 5
    await lib.set_rating(ref, 0)
    assert "userRating" not in server.songs["s1"]
    await lib.set_rating(ref, 2)
    await lib.set_rating(ref, None)  # None -> rating=0 clears
    assert "userRating" not in server.songs["s1"]
    with pytest.raises(FeedbackRejected):
        await lib.set_rating(ref, 6)
    with pytest.raises(FeedbackRejected):
        await lib.set_rating(ItemRef(library_id="other", item_id="s1"), 3)
    with pytest.raises(FeedbackRejected):  # Subsonic error 70
        await lib.set_rating(ItemRef(library_id="nd1", item_id="gone"), 3)


async def test_positions_are_a_noop(lib):
    await lib.set_position(ItemRef(library_id="nd1", item_id="s1"), 30.0, True)
    assert "scrobble" not in lib.transport.calls and not lib.capabilities.supports_positions

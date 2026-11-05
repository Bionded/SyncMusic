"""Navidrome as a FeedbackSink against the local Navidrome: play count +1 per accepted play, rating set
and cleared; the server's listening data is restored afterwards (tests/integration/navidrome_db.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from syncmusic.base.feedback import FeedbackRejected
from syncmusic.core.models import ItemRef, PlayEvent
from tests.integration import navidrome_db
from tests.integration.test_navidrome_local import ENV, navidrome  # noqa: F401 - fixture

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif("NAVIDROME_URL" not in ENV, reason="no local Navidrome in .env.test"),
]


@pytest.fixture
def restored():
    if not navidrome_db.available():
        pytest.skip("Navidrome database not reachable in the compose stack")
    state = navidrome_db.save()
    yield
    navidrome_db.restore(state)
    assert navidrome_db.save() == state


async def test_plays_and_rating_reach_navidrome(navidrome, restored):  # noqa: F811
    snap = await navidrome.snapshot()
    item_id, track = sorted(snap.tracks.items())[0]
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(days=2)
    plays = [
        PlayEvent(ref=track.ref, played_at=start + timedelta(minutes=10 * i)) for i in range(3)
    ]
    missing = PlayEvent(
        ref=ItemRef(library_id=navidrome.id, item_id="no-such-song"), played_at=start
    )
    result = await navidrome.submit_plays([*plays, missing])
    assert result.accepted == plays
    assert [p for p, _ in result.rejected] == [missing] and result.failed == []  # permanent
    after = (await navidrome.snapshot()).tracks[item_id]
    assert after.play_count == track.play_count + 3
    assert after.last_played == plays[-1].played_at  # the scrobble's own time, not "now"
    assert await navidrome.play_count(track.ref) == after.play_count
    assert await navidrome.play_count(missing.ref) is None

    await navidrome.set_rating(track.ref, 4)
    assert (await navidrome.snapshot()).tracks[item_id].rating == 4
    await navidrome.set_rating(track.ref, None)
    assert (await navidrome.snapshot()).tracks[item_id].rating is None
    with pytest.raises(FeedbackRejected):  # Subsonic error 70: permanent
        await navidrome.set_rating(missing.ref, 3)


async def test_star_and_unstar_reach_navidrome(navidrome, restored):  # noqa: F811
    """HiBy hearts → Navidrome starred against the real server, not only FakeNavidrome."""
    snap = await navidrome.snapshot()
    item_id, track = sorted(snap.tracks.items())[0]
    await navidrome.set_starred(track.ref, True)
    assert (await navidrome.snapshot()).tracks[item_id].starred
    await navidrome.set_starred(track.ref, False)
    assert not (await navidrome.snapshot()).tracks[item_id].starred
    # Real Navidrome answers `star` of an unknown id with status ok (no error 70): a stale heart is a harmless no-op.
    await navidrome.set_starred(ItemRef(library_id=navidrome.id, item_id="no-such-song"), True)
    assert "no-such-song" not in (await navidrome.snapshot()).tracks

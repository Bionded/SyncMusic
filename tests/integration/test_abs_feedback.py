"""Audiobookshelf as a FeedbackSink against the local ABS: book chapter and podcast episode positions
reach the user's progress; the progress is restored afterwards (tests/integration/abs_progress.py)."""

from __future__ import annotations

import pytest

from syncmusic.core.models import MediaKind
from tests.integration import abs_progress
from tests.integration.test_abs_local import ENV, SAMPLE_BOOK, abs_lib  # noqa: F401 - fixture

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif("ABS_URL" not in ENV, reason="no local Audiobookshelf in .env.test"),
]


async def test_positions_reach_abs(abs_lib):  # noqa: F811
    saved = await abs_progress.save(abs_lib)
    try:
        snap = await abs_lib.snapshot()
        chapters = sorted(
            (
                t
                for t in snap.tracks.values()
                if t.kind is MediaKind.AUDIOBOOK and t.album != SAMPLE_BOOK
            ),  # 4 s parts
            key=lambda t: (t.album_id or "", t.chapter_index or 0),
        )
        book = [t for t in chapters if t.album_id == chapters[0].album_id]
        assert len(book) >= 3, "a multi-file audiobook is needed"
        chapter = book[1]
        episode = next(t for t in snap.tracks.values() if t.kind is MediaKind.PODCAST)

        await abs_lib.set_position(chapter.ref, 12.5, False)
        await abs_lib.set_position(episode.ref, 33.0, False)
        after = (await abs_lib.snapshot()).tracks
        assert after[chapter.ref.item_id].position_s == pytest.approx(12.5, abs=0.01)
        assert after[book[0].ref.item_id].finished  # chapters before the position count as heard
        assert not after[book[2].ref.item_id].finished
        assert after[episode.ref.item_id].position_s == pytest.approx(33.0, abs=0.01)
        assert not after[episode.ref.item_id].finished

        await abs_lib.set_position(episode.ref, 0.0, True)
        assert (await abs_lib.snapshot()).tracks[episode.ref.item_id].finished
    finally:
        await abs_progress.restore(abs_lib, saved)
    assert abs_progress.same(await abs_progress.save(abs_lib), saved)

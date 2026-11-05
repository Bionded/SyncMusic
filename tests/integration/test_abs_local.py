"""Audiobookshelf library against the test ABS (compose stack, credentials in .env.test) on the generated test media."""

from __future__ import annotations

from collections import defaultdict

import pytest

from syncmusic.core.models import LibraryConfig, MediaKind
from syncmusic.libraries.audiobookshelf import AudiobookshelfLibrary
from tests.integration import synthetic_media
from tests.integration.services import ENV, media_listing

SAMPLE_BOOK = "Tiny Sample Book"  # tests/integration/abs-samples, not test media


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif("ABS_URL" not in ENV, reason="no Audiobookshelf credentials in .env.test"),
]


@pytest.fixture
async def abs_lib(services):
    lib = AudiobookshelfLibrary(
        LibraryConfig(
            id="abs",
            type="audiobookshelf",
            name="ABS",
            settings={"url": ENV.get("ABS_URL", ""), "api_token": ENV.get("ABS_TOKEN", "")},
        ),
        None,
    )
    status = await lib.check()
    if not status.ok:
        pytest.skip(f"Audiobookshelf not available: {status.message}")
    yield lib
    await lib.close()


async def test_audiobooks_and_podcast(abs_lib):
    before = media_listing()
    snap = await abs_lib.snapshot()
    books: dict[str, list] = defaultdict(list)
    for t in snap.tracks.values():
        if t.kind is MediaKind.AUDIOBOOK and t.album != SAMPLE_BOOK:
            books[t.extra["abs_item_id"]].append(t)
    assert len(books) == len(synthetic_media.BOOKS)
    for chapters in books.values():
        chapters.sort(key=lambda t: t.chapter_index)
        assert [t.chapter_index for t in chapters] == list(range(1, len(chapters) + 1))
        offsets = [t.extra["abs_start_offset"] for t in chapters]
        assert offsets == sorted(offsets)  # chapter order = ABS playback order
    good = next(c for c in books.values() if len(c) == 27)
    assert ["24." in t.path for t in good].index(True) == 23  # by track number, not by title
    pods = [t for t in snap.tracks.values() if t.kind is MediaKind.PODCAST]
    assert len(pods) == len(synthetic_media.EPISODES) and len({t.series for t in pods}) == 1
    t = min((t for t in snap.tracks.values() if t.album != SAMPLE_BOOK), key=lambda t: t.size_bytes)
    size = 0
    async for chunk in await abs_lib.open_media(t.ref.item_id):
        size += len(chunk)
    assert size == t.size_bytes
    assert media_listing() == before

"""Folder library on the generated test media (read-only)."""

from __future__ import annotations

from collections import Counter

import pytest

from syncmusic.core.models import LibraryConfig, MediaKind
from syncmusic.libraries.folder import FolderLibrary
from tests.integration import synthetic_media
from tests.integration.services import media_listing, media_root

pytestmark = pytest.mark.integration


async def test_test_media_snapshot_read_only():
    before = media_listing()
    lib = FolderLibrary(
        LibraryConfig(
            id="data",
            type="folder",
            name="Data",
            settings={
                "root": str(media_root()),
                "subfolder_kinds": {"Audiobooks": "audiobook", "Podcasts": "podcast"},
            },
        ),
        None,
    )
    snap = await lib.snapshot()
    kinds = Counter(t.kind for t in snap.tracks.values())
    assert kinds[MediaKind.MUSIC] == synthetic_media.MUSIC_TRACKS
    assert kinds[MediaKind.PODCAST] == len(synthetic_media.EPISODES)
    assert kinds[MediaKind.AUDIOBOOK] == sum(len(parts) for *_, parts in synthetic_media.BOOKS)
    it = sorted(
        (t for t in snap.tracks.values() if "Воно" in (t.path or "")), key=lambda t: t.chapter_index
    )
    assert "Епілог" in it[-1].path  # epilogue last, not first
    good = sorted(
        (t for t in snap.tracks.values() if "Добрі" in (t.path or "")),
        key=lambda t: t.chapter_index,
    )
    assert [t.track_no for t in good] == list(range(1, len(good) + 1))
    lyrics = sum(t.has_lyrics for t in snap.tracks.values() if t.kind is MediaKind.MUSIC)
    assert lyrics == synthetic_media.MUSIC_LYRICS
    assert media_listing() == before  # nothing in Data changed

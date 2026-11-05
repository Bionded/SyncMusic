"""Audiobooks in progress: the book's position from chapter bookmarks, and who is ahead."""

from __future__ import annotations

from syncmusic.core.models import ItemRef, Manifest, ManifestEntry, ManifestHeader, MediaKind
from syncmusic.core.positions import books_on_device, compare
from tests.factories import track


def _entry(i, pos, dur=60.0, kind=MediaKind.AUDIOBOOK):
    return ManifestEntry(
        ref=ItemRef(library_id="abs", item_id=f"b:{i}"),
        device_ref=str(i),
        path=f"Audiobooks/{i:02}.mp3",
        size=1,
        kind=kind,
        duration_s=dur,
        last_position_s=pos,
    )


def _books(*entries, tracks=None):
    m = Manifest(header=ManifestHeader(device_id="d"), items={e.ref.key(): e for e in entries})
    return books_on_device(m, tracks or {})


def test_position_is_the_chapters_before_plus_the_newest_bookmark():
    tracks = {
        f"abs/b:{i}": track(f"b:{i}", library_id="abs", album="Книга", author="Автор", track_no=i)
        for i in (1, 2, 3)
    }
    (b,) = _books(_entry(3, None), _entry(1, 50.0), _entry(2, 10.0), tracks=tracks)
    assert (b.title, b.author, b.position_s, b.duration_s) == ("Книга", "Автор", 70.0, 180.0)
    assert b.first.item_id == "b:1"


def test_unstarted_finished_and_other_kinds_are_left_out():
    assert _books(_entry(1, None)) == []
    assert _books(_entry(1, 60.0)) == []  # at the end: finished
    assert _books(_entry(1, 10.0, kind=MediaKind.PODCAST)) == []


def test_compare():
    assert compare(70, None, None) == ("unknown", 0.0)
    assert compare(70, 80, 20) == ("in_sync", 0.0)
    assert compare(70, 20, 70) == (
        "library_ahead",
        50.0,
    )  # moved back elsewhere: goes onto the device
    assert compare(70, 20, 20) == ("device_ahead", 50.0)
    assert compare(70, 200, None) == ("library_ahead", 130.0)

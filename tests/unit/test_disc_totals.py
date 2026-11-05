"""Disc count of a release: the tag, else the album's highest disc number in the library."""

from __future__ import annotations

from syncmusic.core.models import MediaKind
from syncmusic.core.planner import with_disc_totals
from syncmusic.libraries.folder import _disc_total
from tests.factories import track


def test_disc_total_from_the_album():
    ts = {
        "a": track("a", album="Double", album_id="d", disc_no=1),
        "b": track("b", album="Double", album_id="d", disc_no=2),
        "c": track("c", album="Single", album_id="s", disc_no=1),
        "d": track("d", album="Tagged", album_id="t", disc_no=1, disc_total=3),
        "e": track("e", album="Loose", disc_no=None),
        "f": track("f", album="Double", album_id="d", disc_no=1, kind=MediaKind.AUDIOBOOK),
        "g": track("g", library_id="other", album="Double", album_id="d", disc_no=1),
    }
    out = with_disc_totals(ts)
    got = {k: t.disc_total for k, t in out.items()}
    assert got == {"a": 2, "b": 2, "c": 1, "d": 3, "e": None, "f": None, "g": 1}
    assert ts["a"].disc_total is None  # the snapshot's tracks are not changed


def test_folder_tag_parsing():
    assert _disc_total("2/3", None) == 3
    assert _disc_total("2", "4") == 4
    assert _disc_total("1", None) is None

# SPDX-License-Identifier: GPL-3.0-or-later
"""Hearts, play counts and last played from the library into a Compás card's tagcache, in place.

The player keeps these three numbers in its master index (`.compas/database_idx.tcd[.gN]`, one row of 24 int32 per
file: tag 15 play count, 16 rating - 1 = favourite -, 18 last played) and changes them itself the same way: it
rewrites the 12 bytes of the row. Its later rescans keep them (a re-read file is updated in its slot, the three
numbers stay), so a value we put there lasts. Only rows of files the player has already indexed can be patched;
a file it has not indexed yet gets its values at a later sync. Nothing else in the file changes, and nothing is
written while the index is marked as being written.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path

from syncmusic.devices.HibyR1 import compas_feedback as cf


@dataclass(frozen=True)
class StatsUpdate:
    """What the library says about one of our files; None = leave that number alone."""

    playcount: int | None = None  # raises the player's count, never lowers it
    last_played: int | None = None  # unix seconds; moves it later, never earlier
    favourite: bool | None = None  # True: heart; False: a heart is taken away


@dataclass(frozen=True)
class Patched:
    """A row that was found (and changed when the values differed): the numbers it holds now."""

    playcount: int
    rating: int
    last_played: int


def _merge(e: cf.CompasEntry, u: StatsUpdate) -> tuple[int, int, int]:
    plays = max(e.playcount, u.playcount or 0)
    last = max(e.last_played, u.last_played or 0)
    rating = e.rating
    if u.favourite is True:
        rating = cf.FAVOURITE
    elif u.favourite is False and rating == cf.FAVOURITE:
        rating = 0
    return plays, rating, last


def patch_stats(mount: Path, updates: dict[str, StatsUpdate]) -> dict[str, Patched]:
    """Apply `updates` (card-relative path -> StatsUpdate) to the rows of the current generation. Returns every
    updated path the index has, with its numbers after the patch. Raises cf.CompasDbError when the index is
    missing, of an unknown format, damaged or being written."""
    entries = cf.read_tagcache(mount)
    if entries is None:
        raise cf.CompasDbError("the player has not indexed the card yet")
    path = cf.master_path(mount)
    out: dict[str, Patched] = {}
    writes: list[tuple[int, int]] = []  # (offset, value)
    for e in entries:
        u = updates.get(e.path)
        if u is None or e.slot < 0:
            continue
        plays, rating, last = _merge(e, u)
        out[e.path] = Patched(plays, rating, last)
        row = cf.MASTER_HEADER + e.slot * cf.ENTRY_SIZE
        for tag, old, new in (
            (cf.TAG_PLAYCOUNT, e.playcount, plays),
            (cf.TAG_RATING, e.rating, rating),
            (cf.TAG_LASTPLAYED, e.last_played, last),
        ):
            if new != old:
                writes.append((row + tag * 4, new))
    if writes:
        with open(path, "r+b") as f:
            for offset, value in writes:
                f.seek(offset)
                f.write(struct.pack("<i", value))
            f.flush()
            os.fsync(f.fileno())
    return out

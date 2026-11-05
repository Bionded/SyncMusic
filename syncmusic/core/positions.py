# SPDX-License-Identifier: GPL-3.0-or-later
"""Audiobooks in progress on a device (the device Overview).

The device's position of a book is the one it had at the last sync: the manifest keeps each chapter file's
`last_position_s`; the book's position is the time of the chapters before the newest bookmarked one plus that
bookmark. It is compared with the library's position now and the library's position recorded after the last
reverse sync to tell who is ahead."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from syncmusic.core.models import ItemRef, Manifest, ManifestEntry, MediaKind, Track

NEAR_S = 30.0  # positions closer than this are the same
State = Literal["device_ahead", "in_sync", "library_ahead", "unknown"]


@dataclass
class BookOnDevice:
    library_id: str
    first: ItemRef  # the first chapter: its cover, and the book's scope in the library
    title: str
    author: str
    position_s: float
    duration_s: float


def _book_key(e: ManifestEntry, track: Track | None) -> str:
    if track is not None and (track.album_id or track.album or track.series):
        return f"{e.ref.library_id}\0{track.album_id or track.album or track.series}"
    return e.ref.key()


def _order(e: ManifestEntry, track: Track | None) -> tuple[int, str]:
    n = track and (track.chapter_index if track.chapter_index is not None else track.track_no)
    return (n if isinstance(n, int) else 10**6, e.path)


def books_on_device(manifest: Manifest, tracks: Mapping[str, Track]) -> list[BookOnDevice]:
    """The audiobooks on the device whose position (as of the last sync) is inside the book."""
    groups: dict[str, list[ManifestEntry]] = {}
    for e in manifest.items.values():
        if e.kind is MediaKind.AUDIOBOOK and not e.orphan:
            groups.setdefault(_book_key(e, tracks.get(e.ref.key())), []).append(e)
    out = []
    for entries in groups.values():
        entries.sort(key=lambda e: _order(e, tracks.get(e.ref.key())))
        durations = [e.duration_s or 0.0 for e in entries]
        current = max(
            (i for i, e in enumerate(entries) if (e.last_position_s or 0) > 0), default=None
        )
        if current is None:
            continue
        position = sum(durations[:current]) + (entries[current].last_position_s or 0.0)
        duration = sum(durations)
        if duration and position >= duration - 1:  # at the end: finished
            continue
        first = tracks.get(entries[0].ref.key())
        out.append(
            BookOnDevice(
                library_id=entries[0].ref.library_id,
                first=entries[0].ref,
                title=(first and (first.album or first.series or first.title)) or entries[0].path,
                author=(first and (first.author or first.album_artist or first.artist)) or "",
                position_s=position,
                duration_s=duration,
            )
        )
    return sorted(out, key=lambda b: b.title.casefold())


def compare(
    device_s: float, library_s: float | None, recorded_s: float | None
) -> tuple[State, float]:
    """Who is ahead and by how many seconds. The library moved since the last sync (`recorded_s`): its position
    goes onto the device with the next sync ("Library ahead · will update"), even when it is behind."""
    if library_s is None:
        return "unknown", 0.0
    if abs(library_s - device_s) <= NEAR_S:
        return "in_sync", 0.0
    moved = recorded_s is not None and abs(library_s - recorded_s) > NEAR_S
    if moved or library_s > device_s:
        return "library_ahead", abs(library_s - device_s)
    return "device_ahead", device_s - library_s

# SPDX-License-Identifier: GPL-3.0-or-later
"""FeedbackSink: receives listening data (plays, ratings, positions) from reverse sync."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from syncmusic.core.models import ItemRef, PlayEvent, PositionInfo


class FeedbackRejected(Exception):
    """The sink can never take this data (item deleted, not one of its items, invalid value): a permanent failure.
    Any other exception from a sink is transient (server down, timeout) and the data stays pending indefinitely."""


class SubmitResult(BaseModel):
    accepted: list[PlayEvent] = []
    failed: list[tuple[PlayEvent, str]] = []  # transient: retried at every following run
    rejected: list[
        tuple[PlayEvent, str]
    ] = []  # permanent (FeedbackRejected): dropped after MAX_ATTEMPTS runs


class FeedbackSink(ABC):
    """Mixin for libraries (or other services) that accept listening data."""

    sink_name: str = ""

    @abstractmethod
    async def submit_plays(self, plays: list[PlayEvent]) -> SubmitResult: ...

    async def play_count(self, ref: ItemRef) -> int | None:
        """The item's current play count on the server, None if the sink cannot tell. Read just before a play is
        sent, so a run killed during the send can tell on the next run whether the play arrived;
        None means it is sent again."""
        return None

    @abstractmethod
    async def set_rating(self, ref: ItemRef, rating: int | None) -> None:
        """Raises FeedbackRejected for a permanent failure, anything else for a transient one."""

    @abstractmethod
    async def set_position(self, ref: ItemRef, position_s: float, finished: bool) -> None:
        """Raises FeedbackRejected for a permanent failure, anything else for a transient one."""

    async def set_starred(self, ref: ItemRef, starred: bool) -> None:
        """Favourite on/off, for sinks with `supports_starred`. Raises like `set_rating`."""
        raise FeedbackRejected(f"{self.sink_name or 'this library'} has no favourites")

    async def get_playlist(self, playlist_id: str) -> list[ItemRef] | None:
        """The songs of the playlist `playlist_id` now, in order; None if the sink cannot tell."""
        return None

    async def playlists_by_name(self) -> dict[str, str] | None:
        """Playlist name -> its item id, for playlists created on a device; None: cannot tell."""
        return None

    async def save_playlist(
        self, name: str, refs: list[ItemRef], playlist_id: str | None = None
    ) -> str:
        """Reverse playlist sync: replace the songs of the playlist `playlist_id` (its own item id)
        with `refs` in this order, or create a playlist `name` when `playlist_id` is None. Returns the playlist's item
        id. Sinks without writable playlists raise FeedbackRejected (the default)."""
        raise FeedbackRejected(f"{self.sink_name or 'this library'} cannot store playlists")

    async def get_position(self, ref: ItemRef) -> PositionInfo | None:
        """The item's position on the server (in the item's own time, like `set_position`) with the time it was
        set; None if the sink cannot tell. Read before a device position is sent: when both moved since the last
        sync, the newer one wins; None means the device's position is sent."""
        return None

    async def position_scope(self, ref: ItemRef) -> str:
        """Items with the same scope share one position on the server (ABS: the chapters of a book). Of a device's
        positions in one scope only the newest is compared and sent."""
        return ref.key()

    async def scope_positions(self) -> dict[str, PositionInfo]:
        """The server's position of every scope it has one for (keys as `position_scope`), in the scope's own time
        (ABS: a book's time, not a chapter's), with the time it was set; {} if the sink cannot tell. Recorded per
        device after each reverse sync, so a scope that moved on the server since is seen even when it moved only
        in chapters the device did not touch."""
        return {}


def stores_ratings(library: Any) -> bool:
    """The library takes ratings back (a FeedbackSink with supports_ratings). For any other library the device's
    own rating is the only one: the forward sync never overwrites it (docs/08 Ratings)."""
    caps = getattr(library, "capabilities", None)
    return isinstance(library, FeedbackSink) and bool(getattr(caps, "supports_ratings", False))


def stores_starred(library: Any) -> bool:
    """The library keeps favourites (Subsonic star/unstar): its `Track.starred` is meaningful, so a
    device with hearts may mirror it."""
    caps = getattr(library, "capabilities", None)
    return isinstance(library, FeedbackSink) and bool(getattr(caps, "supports_starred", False))

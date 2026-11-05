# SPDX-License-Identifier: GPL-3.0-or-later
"""Snapshot cache: full library snapshots serialized in SQLite, reused while the
library's state token is unchanged (and, if the library sets one, younger than its `snapshot_max_age`).

Browsing never waits once a snapshot is in memory: `browse()` answers from memory at once and, when the
snapshot was last checked BROWSE_REFRESH ago or was expired by a reverse sync, checks / reloads it in the background
(one load per library at a time); `on_updated` then tells the UI. Only a cold cache (the first load after a start,
or after the library was edited) waits. The sync service refreshes the `due()` ones in the background."""

from __future__ import annotations

import asyncio
import logging
import zlib
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Literal

from syncmusic.base.library import Library
from syncmusic.core.models import ItemRef, LibrarySnapshot, MediaKind, utcnow
from syncmusic.infra.db import Database
from syncmusic.infra.memory import trim

log = logging.getLogger(__name__)

BROWSE_REFRESH = timedelta(minutes=30)
HOURLY = timedelta(hours=1)


def _same(before: LibrarySnapshot | None, after: LibrarySnapshot) -> bool:
    """A reload that brought nothing new (same token and content): nobody needs to hear about it."""
    return (
        before is not None
        and before.state_token == after.state_token
        and before.tracks == after.tracks
        and before.playlists == after.playlists
    )


class SnapshotCache:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._memory: dict[str, LibrarySnapshot] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._checked: dict[
            str, datetime
        ] = {}  # when the library last confirmed (or gave) the memory snapshot
        self._expired: set[str] = (
            set()
        )  # kept for browsing, but reloaded before anything else uses it
        self._refreshing: dict[str, asyncio.Task[None]] = {}
        self.on_updated: Callable[[str], None] | None = (
            None  # library id whose snapshot was reloaded
        )
        # Settings → Libraries: "sync" asks the library before every use, "hourly" trusts a snapshot
        # checked within the last hour, "manual" only reloads on Refresh (force) or after a reverse sync changed it
        self.policy: Literal["sync", "hourly", "manual"] = "sync"
        # Settings `update_change_gap_min`: an expired snapshot is reloaded in the background at most this
        # often (a plan or Refresh still reloads it at once)
        self.expired_gap = timedelta(minutes=15)

    def _trusted(self, library_id: str, now: datetime) -> bool:
        """Use the snapshot in memory without asking the library (the refresh policy)."""
        if library_id not in self._memory or library_id in self._expired:
            return False
        if self.policy == "manual":
            return True
        checked = self._checked.get(library_id)
        return self.policy == "hourly" and checked is not None and now - checked < HOURLY

    @property
    def interval(self) -> timedelta | None:
        """How often snapshots in memory are checked in the background (None: never)."""
        return {"sync": BROWSE_REFRESH, "hourly": HOURLY}.get(self.policy)

    def _fresh(self, lib: Library, snap: LibrarySnapshot, token: str, now: datetime) -> bool:
        if snap.state_token != token:
            return False
        return lib.snapshot_max_age is None or now - snap.taken_at < lib.snapshot_max_age

    async def get(self, lib: Library, *, force: bool = False) -> LibrarySnapshot:
        """The library's snapshot: cached if still valid, otherwise reloaded and stored."""
        key = lib.cache_key
        async with self._locks.setdefault(key, asyncio.Lock()):
            if not force and key not in self._expired:
                now = utcnow()
                if self._trusted(key, now):
                    return self._memory[key]
                token = await lib.state_token()
                snap = self._memory.get(key) or await self.load(key)
                if snap is not None and self._fresh(lib, snap, token, now):
                    self._memory[key] = snap
                    self._checked[key] = now
                    return snap
            snap = await lib.snapshot()
            await self.store(snap, key)
            self._expired.discard(key)
            self._checked[key] = utcnow()
            log.info("library %s: snapshot reloaded (%d tracks)", key, len(snap.tracks))
            await asyncio.to_thread(trim)  # the old snapshot's memory back to the OS
            return snap

    async def browse(self, lib: Library) -> tuple[LibrarySnapshot, datetime]:
        """(snapshot, when it was last confirmed) for the library browser: from memory without waiting - a stale
        one starts a background reload - or loaded now when nothing is in memory."""
        key = lib.cache_key
        snap = self._memory.get(key)
        checked = self._checked.get(key)
        if snap is None or checked is None:
            snap = await self.get(lib)
            return snap, self._checked.get(key, snap.taken_at)
        every = self.interval
        if key in self._expired or (every is not None and utcnow() - checked >= every):
            self.refresh_soon(lib)
        return snap, checked

    def refresh_soon(self, lib: Library) -> asyncio.Task[None]:
        """Check / reload the library's snapshot in the background; one at a time per library."""
        key = lib.cache_key
        task = self._refreshing.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._refresh(lib), name=f"snapshot-{key}")
            self._refreshing[key] = task
        return task

    async def _refresh(self, lib: Library) -> None:
        key = lib.cache_key
        before = self._memory.get(key)
        try:
            snap = await self.get(lib)
        except Exception as e:  # noqa: BLE001 - unreachable now: keep serving the last snapshot, try again later
            log.warning("library %s: background refresh failed: %s", key, lib.failure_reason(e))
            self._checked[key] = utcnow()
            return
        if snap is not before and not _same(before, snap) and self.on_updated is not None:
            self.on_updated(lib.id)

    def due(self) -> list[str]:
        """Libraries with a snapshot in memory whose last check is `interval` old (the periodic refresh)."""
        now, every = utcnow(), self.interval
        return [
            lid
            for lid in self._memory
            if "@" not in lid  # a device's own account is checked when that device plans
            and (
                (
                    lid in self._expired
                    and now - self._checked.get(lid, now - self.expired_gap) >= self.expired_gap
                )
                or (every is not None and now - self._checked.get(lid, now - every) >= every)
            )
        ]

    def checked_at(self, library_id: str) -> datetime | None:
        return self._checked.get(library_id) if library_id in self._memory else None

    def counts(self, library_id: str) -> dict[str, int] | None:
        """What the library holds per kind, from the snapshot in memory only (None: not loaded; never loads one - the Libraries list asks this for every library): music tracks, audiobooks (one per book:
        album id or name), podcast episodes, playlists."""
        snap = self._memory.get(library_id)
        if snap is None:
            return None
        music = episodes = videos = 0
        books: set[str] = set()
        for tr in snap.tracks.values():
            if tr.kind == MediaKind.AUDIOBOOK:
                books.add(tr.album_id or tr.album or tr.ref.item_id)
            elif tr.kind == MediaKind.PODCAST:
                episodes += 1
            elif tr.kind == MediaKind.VIDEO:
                videos += 1
            else:
                music += 1
        return {
            "tracks": music,
            "books": len(books),
            "episodes": episodes,
            "videos": videos,
            "playlists": len(snap.playlists),
        }

    async def cached(self, library_id: str) -> LibrarySnapshot | None:
        """The last stored snapshot, without asking the library whether it is still valid (it may be stale)."""
        snap = self._memory.get(library_id) or await self.load(library_id)
        if snap is not None:
            self._memory[library_id] = snap
        return snap

    async def load(self, library_id: str) -> LibrarySnapshot | None:
        row = await self.db.fetchone(
            "SELECT data FROM snapshots WHERE library_id = ?", (library_id,)
        )
        if row is None:
            return None
        try:
            raw = await asyncio.to_thread(zlib.decompress, row["data"])
            return LibrarySnapshot.model_validate_json(raw)
        except (zlib.error, ValueError):
            log.warning("library %s: cached snapshot unreadable, ignoring it", library_id)
            return None

    async def store(self, snap: LibrarySnapshot, key: str | None = None) -> None:
        data = await asyncio.to_thread(lambda: zlib.compress(snap.model_dump_json().encode(), 6))
        await self.db.execute(
            "INSERT INTO snapshots (library_id, state_token, taken_at, data) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(library_id) DO UPDATE SET state_token = excluded.state_token, "
            "taken_at = excluded.taken_at, data = excluded.data",
            (key or snap.library_id, snap.state_token, snap.taken_at.isoformat(), data),
        )
        self._memory[key or snap.library_id] = snap

    async def invalidate(self, library_id: str) -> None:
        """The library was edited or removed: nothing of it may be shown any more (nor of devices' own accounts)."""
        for key in self._keys(library_id):
            self._memory.pop(key, None)
            self._checked.pop(key, None)
            self._expired.discard(key)
        await self.db.execute(
            "DELETE FROM snapshots WHERE library_id = ? OR library_id LIKE ?",
            (library_id, library_id + "@%"),
        )

    async def forget(self, key: str) -> None:
        """One cached snapshot goes (a device's own account was changed or removed)."""
        self._memory.pop(key, None)
        self._checked.pop(key, None)
        self._expired.discard(key)
        await self.db.execute("DELETE FROM snapshots WHERE library_id = ?", (key,))

    def _keys(self, library_id: str) -> list[str]:
        """The library's cache keys in memory: its own and those of devices' accounts (`<id>@<device>`)."""
        keys = {*self._memory, *self._checked, *self._expired}
        return [library_id, *(k for k in keys if k.startswith(library_id + "@"))]

    async def patch(
        self, kind: str, ref: ItemRef, data: dict[str, Any], key: str | None = None
    ) -> None:
        """A reverse sync delivered one row: the snapshot in memory gets the library's new value at
        once - a plan right after (even of a run cancelled before `expire`) sees the episode as played - and the
        stored copy goes, so a restart reloads it."""
        key = key or ref.library_id  # a device's own account: its key
        snap = self._memory.get(key)
        track = snap.tracks.get(ref.item_id) if snap is not None else None
        if snap is not None and track is not None:
            update: dict[str, Any] = {}
            if kind == "play":
                played = datetime.fromisoformat(str(data["played_at"]))
                update = {"play_count": track.play_count + 1, "last_played": played}
            elif kind == "rating":
                update = {"rating": data["rating"]}
            elif kind == "star":
                update = {"starred": bool(data["starred"])}
            elif kind == "position":
                update = {
                    "position_s": float(data["position_s"]),
                    "finished": bool(data.get("finished")),
                }
            # a copy: plans under review hold the old Track
            snap.tracks[ref.item_id] = track.model_copy(update=update)
        await self.db.execute("DELETE FROM snapshots WHERE library_id = ?", (key,))

    async def expire(self, library_id: str) -> None:
        """Its data changed (a reverse sync wrote plays / ratings / positions): the next `get` reloads it, browsing
        keeps showing the last snapshot until the background reload is done."""
        self._expired.add(library_id)
        await self.db.execute("DELETE FROM snapshots WHERE library_id = ?", (library_id,))

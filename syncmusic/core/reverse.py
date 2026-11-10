# SPDX-License-Identifier: GPL-3.0-or-later
"""Reverse sync: listening data from a device to its libraries. Step 0 of every sync and a job of its own.

Collect: `device.read_feedback(manifest)` → plays, ratings, positions (ItemRefs via the manifest).
Capture: everything new goes to `pending_feedback` in one transaction, together with the device's absolute play
counts in `feedback_journal`, **before** the device is consumed.
Consume: manifest `last_*` values + `device.reset_feedback`; then the journal is cleared. If that fails (unplugged,
killed) the next run caps the plays it captures by the journal's counts, so plays whose timestamps are estimated
anew on every read (tagcache deltas, iPod Play Counts without a time) are not captured twice.
Deliver: from the database, oldest first, one item at a time; each row is deleted (and a play marked in
`submitted_plays`) as soon as its library took it. A FeedbackSink takes what its capabilities support; a kind the
sink cannot store goes to `local_feedback`. Before a play is sent the sink's play count
is stored with it (`inflight`): a run killed during the send compares it on the next run instead of sending twice.
Orphans (manifest entries whose library no longer has the item) and items of libraries that are not a
FeedbackSink (folder libraries get no reverse sync) are skipped: their data is consumed from
the device but never captured. An item the sink rejects permanently (FeedbackRejected / SubmitResult.rejected: a song deleted from the library
can never take its plays) is dropped after MAX_ATTEMPTS such runs; transient failures (server down, library
unavailable) keep it pending indefinitely.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from syncmusic.base.capabilities import LibraryCapabilities
from syncmusic.base.device import Device
from syncmusic.base.feedback import FeedbackRejected, FeedbackSink
from syncmusic.base.library import Library
from syncmusic.core.manifest import is_external
from syncmusic.core.models import (
    DeviceConfig,
    DeviceFeedback,
    ItemRef,
    ListeningChange,
    Manifest,
    MediaKind,
    PlayEvent,
    PositionInfo,
    ReverseReport,
    ReverseSyncSettings,
)
from syncmusic.core.playlist_reverse import Policy, push_playlists
from syncmusic.infra.store import PendingRow, Store

log = logging.getLogger(__name__)

FINISHED_WITHIN_S = (
    30.0  # a device bookmark this close to the end means the episode was played to the end
)
# after a play to the end the iPod resets the bookmark (or keeps a few seconds); a bookmark past this is re-listening
RESTARTED_AFTER_S = 60.0
MAX_ATTEMPTS = 10  # runs with a permanent rejection before a pending item is dropped (logged)
MAX_TRANSIENT = (
    3  # consecutive transient failures before the rest of a library's rows wait for the next run
)


class _PlayRejected(FeedbackRejected):
    """A play in SubmitResult.rejected (the message is the sink's own)."""


class _PlayFailed(Exception):
    """A play in SubmitResult.failed (the message is the sink's own)."""


def _message(e: BaseException) -> str:
    return str(e) if isinstance(e, (_PlayRejected, _PlayFailed)) else f"{type(e).__name__}: {e}"


@dataclass
class _Outcome:
    device_id: str = ""
    report: ReverseReport = field(default_factory=ReverseReport)
    errors: set[str] = field(
        default_factory=set
    )  # delivery errors (rows kept pending), reported once each
    superseded: set[str] = field(default_factory=set)  # keys whose newer library position wins
    # the library position per scope after the device's last reverse sync, and now (per library)
    known_scopes: dict[str, float] = field(default_factory=dict)
    # the snapshot cache key per library id: a device's own account has its own
    cache_keys: dict[str, str] = field(default_factory=dict)
    scopes_now: dict[str, dict[str, PositionInfo]] = field(default_factory=dict)
    # why a library is not available now, by id (`ReverseSync.run`'s `unavailable`)
    unavailable: dict[str, str] = field(default_factory=dict)


def _device_time(r: PendingRow) -> datetime:
    """When the device set a position row's value; without a time the oldest."""
    at = r.data.get("updated_at")
    return datetime.fromisoformat(str(at)) if at else datetime.min.replace(tzinfo=UTC)


def _takes_stars(lib: Library | None) -> bool:
    """The library keeps favourites; an unavailable one (None) gets them later."""
    return lib is None or bool(getattr(lib.capabilities, "supports_starred", False))


def _ref_of(kind: str, data: dict[str, Any]) -> ItemRef:
    return PlayEvent.model_validate(data).ref if kind == "play" else ItemRef.parse(str(data["ref"]))


def _rows_from_feedback(
    fb: DeviceFeedback,
    last_positions: Mapping[str, float | None],
    last_ratings: Mapping[str, int | None] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    out = [("play", p.model_dump(mode="json")) for p in fb.plays]
    # the library's rating at the last sync goes along: an undo of this run sends it back
    out += [
        ("rating", {"ref": k, "rating": v, "previous": (last_ratings or {}).get(k)})
        for k, v in fb.ratings.items()
    ]
    out += [("star", {"ref": k, "starred": v}) for k, v in fb.stars.items()]
    # oldest first: the newest position of a book is the one a sink keeps (ABS has one progress per book)
    positions = sorted(
        fb.positions.items(),
        key=lambda kv: (kv[1].updated_at is not None, kv[1].updated_at or datetime.min, kv[0]),
    )
    out += [
        (
            "position",
            {
                "ref": k,
                "position_s": p.position_s,
                "finished": p.finished,
                # when the device's position was set, and the one both sides had after the last sync
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
                "last_s": last_positions.get(k),
            },
        )
        for k, p in positions
    ]
    return out


def mark_finished(
    fb: DeviceFeedback, plays: list[PlayEvent], manifest: Manifest | None
) -> dict[str, float | None]:
    """Podcasts and audiobooks played to the end on the device become a finished position: the
    play count went up (and the bookmark is reset or small, not re-listened past RESTARTED_AFTER_S), or the bookmark
    of a podcast episode is within FINISHED_WITHIN_S (at most 5 %) of the end. Changes `fb.positions`; answers the
    marked items with the device's own bookmark (None: none), which the manifest keeps as the last known one - the
    small bookmark the iPod keeps after the end is then no change on the next sync."""
    if manifest is None:
        return {}
    played: dict[str, datetime] = {}
    for p in plays:
        k = p.ref.key()
        played[k] = max(played.get(k, p.played_at), p.played_at)
    marked: dict[str, float | None] = {}
    for key, entry in manifest.items.items():
        if entry.kind is MediaKind.MUSIC or not entry.duration_s:
            continue
        end = entry.duration_s
        pos = fb.positions.get(key)
        # a chapter's end is not its book's end: the bookmark rule is for episodes; short ones get a smaller margin
        margin = min(FINISHED_WITHIN_S, end * 0.05)
        near_end = (
            entry.kind is MediaKind.PODCAST and pos is not None and pos.position_s >= end - margin
        )
        played_through = key in played and (pos is None or pos.position_s <= RESTARTED_AFTER_S)
        if not (near_end or played_through):
            continue
        if pos is None and (entry.last_position_s or 0) >= end - FINISHED_WITHIN_S:
            continue  # already finished at the last sync: nothing new to say
        at = pos.updated_at if pos is not None and pos.updated_at else played.get(key)
        fb.positions[key] = PositionInfo(position_s=end, finished=True, updated_at=at)
        marked[key] = pos.position_s if pos is not None else None
    return marked


def _uncaptured_plays(fb: DeviceFeedback, journal: dict[str, int]) -> list[PlayEvent]:
    """Plays not captured by an earlier run whose device commit failed: per item at most the device's play count
    minus the count already captured (the newest plays); items without both counts are kept as they are."""
    per_key: dict[str, list[PlayEvent]] = defaultdict(list)
    for p in fb.plays:
        per_key[p.ref.key()].append(p)
    keep: set[int] = set()
    for key, plays in per_key.items():
        if key in journal and key in fb.play_counts:
            allowed = max(0, fb.play_counts[key] - journal[key])
            plays = (
                sorted(plays, key=lambda p: p.played_at)[len(plays) - allowed :] if allowed else []
            )
        keep.update(id(p) for p in plays)
    return [p for p in fb.plays if id(p) in keep]


async def _scope_positions(sink: FeedbackSink) -> dict[str, PositionInfo]:
    try:
        return await sink.scope_positions()
    except Exception:  # noqa: BLE001 - unknown now: decided per item as before
        return {}


async def _covered_positions(sink: FeedbackSink, rows: list[PendingRow]) -> dict[int, PendingRow]:
    """Position rows that share a scope with a newer row (the chapters of a book listened across a boundary) -> that newest row. Only the newest is compared and sent: a scope is one position on the server,
    so the sync's own push of one chapter must not look like a newer library position for another."""
    by_scope: dict[str, list[PendingRow]] = defaultdict(list)
    for r in rows:
        if r.kind != "position":
            continue
        ref = _ref_of(r.kind, r.data)
        try:
            scope = await sink.position_scope(ref)
        except Exception:  # noqa: BLE001 - unknown: the row is its own scope
            scope = ref.key()
        by_scope[scope].append(r)
    covered: dict[int, PendingRow] = {}
    for group in by_scope.values():
        # newest device time wins; without a time a row is the oldest; rows come oldest first
        order = sorted(enumerate(group), key=lambda ir: (_device_time(ir[1]), ir[0]))
        newest = order[-1][1]
        covered.update({r.id: newest for _, r in order[:-1]})
    return covered


def _kept_plays(manifest: Manifest | None) -> list[PlayEvent]:
    """Plays of our items that another installation read from the device (`pending_plays`). Their time is
    unknown: estimated as that installation's write time, one second apart, the same on every run (an interrupted
    run finds the same plays again, and delivery skips what already arrived)."""
    if manifest is None:
        return []
    at = manifest.header.updated_at
    out = []
    for e in manifest.items.values():
        if e.pending_plays and not is_external(e.ref.library_id) and not e.orphan:
            out += [
                PlayEvent(ref=e.ref, played_at=at - timedelta(seconds=n), played_at_estimated=True)
                for n in range(e.pending_plays)
            ]
    return out


def _new_feedback(
    fb: DeviceFeedback,
    plays: list[PlayEvent],
    manifest: Manifest | None,
    libraries: Mapping[str, Library | None],
    settings: ReverseSyncSettings,
    device_wins: bool,
) -> DeviceFeedback:
    """The part of the device's feedback this run captures, by the device's reverse sync settings."""
    # nowhere to go, so consumed but never captured: orphans (gone from their library) and items of
    # libraries without reverse sync (folder libraries)
    skip = {k for k, e in manifest.items.items() if e.orphan} if manifest is not None else set()
    no_sink = {
        lid
        for lid, lib in libraries.items()
        if lib is not None and not isinstance(lib, FeedbackSink)
    }

    def wanted(key: str) -> bool:
        lid = key.split("/", 1)[0]
        return key not in skip and lid not in no_sink and not is_external(lid)

    return DeviceFeedback(
        plays=[p for p in plays if wanted(p.ref.key())] if settings.plays else [],
        # library priority: the device's rating is replaced by the forward sync, never pushed
        ratings={k: v for k, v in fb.ratings.items() if wanted(k)}
        if settings.ratings and device_wins
        else {},
        positions={k: v for k, v in fb.positions.items() if wanted(k)}
        if settings.positions
        else {},
        # a heart is the user's own change on the player: sent whatever the rating priority;
        # libraries without favourites (ABS, folders) ignore them
        stars={
            k: v
            for k, v in fb.stars.items()
            if wanted(k) and _takes_stars(libraries.get(k.split("/", 1)[0]))
        }
        if settings.ratings
        else {},
    )


# removed favourites/ratings at once from which a reset of the player's database is suspected
MASS_UNSTAR_MIN = 10


def _mass_removal(removed: list[str], had: set[str]) -> bool:
    return len(removed) >= MASS_UNSTAR_MIN and len(removed) * 2 > len(had | set(removed))


def _mass_unstar(new: DeviceFeedback, manifest: Manifest | None) -> list[str]:
    """Favourite removals that look like a reset player database, not the user: at least MASS_UNSTAR_MIN and
    more than half of the hearts the device had (those mirrored onto it, plus the removed ones). These are
    dropped from `new` and returned; the user's removals of a few hearts still go."""
    removed = [k for k, v in new.stars.items() if not v]
    if manifest is None or not _mass_removal(
        removed, {k for k, e in manifest.items.items() if e.last_starred}
    ):
        return []
    for k in removed:
        del new.stars[k]
    return [k for k in removed if k in manifest.items]


def _mass_unrate(fb: DeviceFeedback, new: DeviceFeedback, manifest: Manifest | None) -> int:
    """Ratings cleared at once the same way (a reset iTunesDB or tagcache): dropped from the device's feedback
    and from what is sent, so the library keeps them; the manifest forgets the last rating so the forward sync
    writes the library's ratings back to the device."""
    removed = [k for k, v in fb.ratings.items() if v is None]
    if manifest is None or not _mass_removal(
        removed, {k for k, e in manifest.items.items() if e.last_rating}
    ):
        return 0
    for k in removed:
        fb.ratings.pop(k, None)
        new.ratings.pop(k, None)
        if k in manifest.items:
            manifest.items[k].last_rating = None
    return len(removed)


class ReverseSync:
    def __init__(self, store: Store) -> None:
        self.store = store
        # called after each row a library took (kind, ref, row data): the sync patches its cached snapshot right
        # away, so a run cancelled later still plans from what the library holds now; the last
        # argument is the snapshot's cache key (a device's own account)
        self.on_delivered: Callable[[str, ItemRef, dict[str, Any], str], Awaitable[None]] | None = (
            None
        )

    async def _took(self, kind: str, ref: ItemRef, data: dict[str, Any], out: _Outcome) -> None:
        if kind != "position" or data.get("finished"):
            out.report.consumed.append(ref.key())
        if self.on_delivered is not None:
            key = out.cache_keys.get(ref.library_id, ref.library_id)
            await self.on_delivered(kind, ref, data, key)

    async def run(
        self,
        cfg: DeviceConfig,
        device: Device,
        libraries: Mapping[str, Library | None],
        unavailable: Mapping[str, str] | None = None,
        playlist_choices: Mapping[str, Policy] | None = None,
    ) -> ReverseReport:
        """Collect, capture, consume, deliver. `libraries`: the device's libraries by id (None = not available
        now); `unavailable`: why, by library id, named and with the cause ("Navidrome is not reachable: …", the error kept on its pending rows)."""

        settings = cfg.reverse_sync
        if not settings.enabled:
            return ReverseReport()
        manifest = await device.read_manifest()
        fb = await device.read_feedback(manifest) if manifest is not None else DeviceFeedback()
        journal = await self.store.feedback_journal(cfg.id)
        plays = _uncaptured_plays(fb, journal) if journal else fb.plays
        # another installation's items: their plays are kept on the manifest entry for it (see _commit), the
        # plays it kept for ours are sent now
        plays = [p for p in plays if not is_external(p.ref.library_id)]
        plays += _kept_plays(manifest)
        device_wins = cfg.sync.rating_priority == "device"
        # every play the device shows, captured before or not: a run cancelled before the device commit finds the
        # same plays again and must not push the reset bookmark after the finished position
        finished = mark_finished(fb, fb.plays, manifest)
        if finished:
            log.info("%s: %d podcasts/audiobooks played to the end", cfg.id, len(finished))
        new = _new_feedback(fb, plays, manifest, libraries, settings, device_wins)
        reset_hearts = _mass_unstar(new, manifest)
        reset_ratings = _mass_unrate(fb, new, manifest)
        if reset_ratings:
            log.warning(
                "%s: %d ratings cleared at once - treated as a reset player database, none sent",
                cfg.id,
                reset_ratings,
            )
        if reset_hearts:
            log.warning(
                "%s: %d favourites removed at once - treated as a reset player database, none sent",
                cfg.id,
                len(reset_hearts),
            )
        skipped = len(plays) - len(new.plays) if settings.plays else 0
        if skipped:
            log.info(
                "%s: %d plays skipped (orphans or libraries without reverse sync)", cfg.id, skipped
            )
        out = _Outcome(
            device_id=cfg.id,
            unavailable=dict(unavailable or {}),
            cache_keys={lid: lib.cache_key for lid, lib in libraries.items() if lib is not None},
        )
        out.report.plays_found = len(fb.plays)
        if len(plays) < len(fb.plays):
            log.info("%s: %d plays were already captured", cfg.id, len(fb.plays) - len(plays))
        if settings.plays or settings.ratings:
            out.report.warnings = await device.feedback_warnings()
        if reset_hearts:
            out.report.held_unstars = list(reset_hearts)
            out.report.warnings.append(
                f"The player's favourites were reset ({len(reset_hearts)} removed at once): SyncMusic did "
                "not remove them from the library and writes them back to the player"
            )
        if reset_ratings:
            out.report.warnings.append(
                f"The player's ratings were reset ({reset_ratings} cleared at once): SyncMusic did not "
                "clear them in the library"
            )
        if fb.unmapped:
            log.info("%s: %d feedback entries not in the manifest (ignored)", cfg.id, fb.unmapped)

        last_positions = (
            {k: e.last_position_s for k, e in manifest.items.items()} if manifest else {}
        )
        last_ratings = {k: e.last_rating for k, e in manifest.items.items()} if manifest else {}
        await self._capture(
            cfg.id,
            new,
            libraries,
            fb.play_counts if manifest else {},
            last_positions,
            out,
            last_ratings,
        )
        playlists_changed = False
        if manifest is not None and settings.playlists:
            try:
                playlists_changed = await push_playlists(
                    device,
                    manifest,
                    libraries,
                    out.report,
                    cfg.sync.playlist_conflicts,
                    cfg.name,
                    playlist_choices,
                )
            except Exception as e:  # noqa: BLE001 - listening data still goes; the edits stay for the next run
                log.warning("%s: device playlists not read: %s", cfg.id, e)
                out.report.errors.append(f"playlists: {e}")
        if manifest is not None:
            # the manifest keeps the device's own bookmark of a finished item, not its end
            kept = {k: p for k, p in fb.positions.items() if k not in finished}
            kept |= {k: PositionInfo(position_s=v) for k, v in finished.items() if v is not None}
            for key in reset_hearts:  # the forward sync writes the library's hearts back
                manifest.items[key].last_starred = None
            await self._commit(
                device,
                manifest,
                fb.model_copy(update={"positions": kept}),
                playlists_changed or bool(reset_hearts) or bool(reset_ratings),
            )
            if journal is not None or fb.play_counts:
                await self.store.clear_feedback_journal(cfg.id)
        device_positions = await self._deliver_pending(cfg.id, libraries, settings, out)
        await self._report_pending(cfg.id, fb, settings, device_wins, device_positions, out)
        out.report.errors += sorted(out.errors)[:20]
        log.info("%s: reverse sync: %s", cfg.id, summary(out.report))
        return out.report

    async def _deliver_pending(
        self,
        device_id: str,
        libraries: Mapping[str, Library | None],
        settings: ReverseSyncSettings,
        out: _Outcome,
    ) -> dict[str, float]:
        """Deliver every pending row (this run's and older ones) to its library. Returns the device positions
        that were pending before delivery, by item key."""
        by_lib: dict[str, list[PendingRow]] = defaultdict(list)
        # device positions not in the library before this run (new or pending): the plan's library values for
        # them are stale, the forward sync must not move those bookmarks
        device_positions: dict[str, float] = {}
        for r in await self.store.pending_feedback(device_id):  # oldest first
            by_lib[_ref_of(r.kind, r.data).library_id].append(r)
            if r.kind == "position":
                device_positions[str(r.data["ref"])] = float(r.data["position_s"])
        if settings.positions:
            out.known_scopes = await self.store.position_scopes(device_id)
        for lib_id, rows in by_lib.items():
            await self._deliver(lib_id, libraries, rows, out)
        if settings.positions:
            await self._record_scopes(device_id, libraries)
        return device_positions

    async def _report_pending(
        self,
        device_id: str,
        fb: DeviceFeedback,
        settings: ReverseSyncSettings,
        device_wins: bool,
        device_positions: Mapping[str, float],
        out: _Outcome,
    ) -> None:
        """What is still pending, and the device's values the forward sync must keep."""
        rest = await self.store.pending_feedback(device_id)
        out.report.pending = len(rest)
        for r in rest:  # the report's "N plays waiting for <library>" line
            per = out.report.pending_by.setdefault(_ref_of(r.kind, r.data).library_id, {})
            per[r.kind] = per.get(r.kind, 0) + 1
        if settings.ratings:
            out.report.device_ratings = dict(fb.ratings)
            if device_wins:  # not delivered yet: still the device's value, not the library's
                for r in rest:
                    if r.kind == "rating":
                        out.report.device_ratings.setdefault(str(r.data["ref"]), r.data["rating"])
        if settings.positions:
            out.report.device_positions = {
                k: pos
                for k, pos in (
                    dict(device_positions) | {k: p.position_s for k, p in fb.positions.items()}
                ).items()
                if k
                not in out.superseded  # the forward sync writes the newer library position instead
            }

    # --- capture ---------------------------------------------------------------------------------

    async def _capture(
        self,
        device_id: str,
        new: DeviceFeedback,
        libraries: Mapping[str, Library | None],
        play_counts: dict[str, int],
        last_positions: Mapping[str, float | None],
        out: _Outcome,
        last_ratings: Mapping[str, int | None] | None = None,
    ) -> None:
        """New data → pending rows + the journal of the device's play counts, in one transaction. A pending
        rating/position superseded by a newer device value is dropped, so is data of a library no longer attached
        to the device."""
        rows: list[tuple[str, dict[str, Any]]] = []
        detached: dict[str, int] = defaultdict(int)
        for kind, data in _rows_from_feedback(new, last_positions, last_ratings):
            lib_id = _ref_of(kind, data).library_id
            if lib_id in libraries:
                rows.append((kind, data))
            else:
                detached[lib_id] += 1
        for lib_id, n in detached.items():
            out.report.errors.append(
                f"{n} entries for library {lib_id!r} dropped: not attached to the device"
            )
        newer = {(kind, str(data["ref"])) for kind, data in rows if kind != "play"}
        superseded = [
            r.id
            for r in await self.store.pending_feedback(device_id)
            if r.kind != "play" and (r.kind, str(r.data["ref"])) in newer
        ]
        if rows or superseded or play_counts:
            await self.store.capture_feedback(device_id, rows, superseded, play_counts or None)

    # --- deliver ---------------------------------------------------------------------------------

    async def _deliver(
        self,
        lib_id: str,
        libraries: Mapping[str, Library | None],
        rows: list[PendingRow],
        out: _Outcome,
    ) -> None:
        if lib_id not in libraries:
            await self.store.delete_pending([r.id for r in rows])
            out.report.errors.append(
                f"{len(rows)} entries for library {lib_id!r} dropped: not attached to the device"
            )
            return
        lib = libraries[lib_id]
        if lib is None:
            why = out.unavailable.get(lib_id, f"library {lib_id!r} is not available")
            for r in rows:
                await self._failed(r, why, False, out)
            return
        if not isinstance(
            lib, FeedbackSink
        ):  # no reverse sync for it (folder): data from before goes
            await self.store.delete_pending([r.id for r in rows])
            log.info(
                "%d entries for library %s dropped: it takes no listening data", len(rows), lib_id
            )
            return
        sink = lib
        covered = (
            await _covered_positions(sink, rows) if lib.capabilities.supports_positions else {}
        )
        if out.known_scopes and any(r.kind == "position" for r in rows):
            out.scopes_now[lib_id] = await _scope_positions(sink)
        transient = 0
        done: set[int] = set()
        for r in rows:
            if r.id in covered:
                continue
            if transient >= MAX_TRANSIENT:  # the server is down: the rest waits for the next run
                await self._failed(r, f"{lib.config.name} is not reachable", False, out)
                continue
            try:
                await self._deliver_one(lib_id, sink, lib.capabilities, r, out)
            except FeedbackRejected as e:
                await self._failed(r, _message(e), True, out)
            except Exception as e:  # noqa: BLE001 - any other sink error keeps the data pending
                transient += 1
                await self._failed(r, _message(e), False, out)
            else:
                transient = 0
                done.add(r.id)
        for (
            r
        ) in rows:  # older positions of a scope follow its newest one; pending while that one is
            newest = covered.get(r.id)
            if newest is None or newest.id not in done:
                continue
            if _ref_of(newest.kind, newest.data).key() in out.superseded:
                await self.store.delete_pending([r.id])
                out.superseded.add(_ref_of(r.kind, r.data).key())
                out.report.positions_superseded += 1
            else:
                await self.store.delivered(r.id)

    async def _deliver_one(
        self,
        lib_id: str,
        sink: FeedbackSink | None,
        caps: LibraryCapabilities,
        r: PendingRow,
        out: _Outcome,
    ) -> None:
        """Deliver one row; raises FeedbackRejected (permanent) or anything else (transient) on failure."""
        ref = _ref_of(r.kind, r.data)
        if r.kind == "play":
            play = PlayEvent.model_validate(r.data)
            if sink is not None and caps.supports_scrobble:
                if await self._send_play(sink, r, play, out):
                    await self._took(r.kind, ref, r.data, out)
            else:
                await self.store.add_local_feedback(
                    lib_id, ref.item_id, plays=1, last_played=play.played_at, done_row=r.id
                )
        elif r.kind == "rating":
            rating = r.data["rating"]
            if sink is not None and caps.supports_ratings:
                await sink.set_rating(ref, rating)
                await self.store.delivered(r.id)
                out.report.ratings_pushed += 1
                out.report.changes.append(
                    ListeningChange(
                        kind="rating", ref=ref.key(), value=rating, previous=r.data.get("previous")
                    )
                )
                await self._took(r.kind, ref, r.data, out)
            else:
                await self.store.add_local_feedback(
                    lib_id, ref.item_id, rating=rating, set_rating=True, done_row=r.id
                )
        elif r.kind == "star":
            if sink is not None and caps.supports_starred:
                starred = bool(r.data["starred"])
                await sink.set_starred(ref, starred)
                out.report.stars_pushed += 1
                out.report.changes.append(
                    ListeningChange(kind="star", ref=ref.key(), value=starred, previous=not starred)
                )
                await self._took(r.kind, ref, r.data, out)
            await self.store.delivered(r.id)
        elif r.kind == "position":
            pos, finished = float(r.data["position_s"]), bool(r.data.get("finished"))
            if sink is not None and caps.supports_positions:
                if await self._library_newer(sink, ref, r.data, out):
                    await self.store.delete_pending([r.id])
                    out.superseded.add(ref.key())
                    out.report.positions_superseded += 1
                    return
                await sink.set_position(ref, pos, finished)
                await self.store.delivered(r.id)
                out.report.positions_pushed += 1
                out.report.finished_pushed += finished
                await self._took(r.kind, ref, r.data, out)
            else:
                await self.store.add_local_feedback(
                    lib_id, ref.item_id, position_s=pos, done_row=r.id
                )

    async def _library_newer(
        self, sink: FeedbackSink, ref: ItemRef, data: dict[str, Any], out: _Outcome
    ) -> bool:
        """Both sides moved since the last sync and the library's position is the newer one. Unknown
        times (device or library) keep the old rule: the device's position wins. Whether the library moved is
        decided on its whole scope (a book) when the position after the last sync is known, else on the
        item's own position (a chapter's, clamped to it)."""
        if not data.get("updated_at"):
            return False
        now = out.scopes_now.get(ref.library_id, {})
        scope = await sink.position_scope(ref) if now else None
        lib: PositionInfo | None
        if scope is not None and scope in now and scope in out.known_scopes:
            lib = now[scope]
            if abs(lib.position_s - out.known_scopes[scope]) < 1:
                return False  # the scope did not move on the server: the device's position is the only change
        else:
            lib = await sink.get_position(ref)
            last = data.get("last_s")
            if lib is not None and last is not None and abs(lib.position_s - float(last)) < 1:
                return False  # the library did not move: the device's position is the only change
        if lib is None or lib.updated_at is None:
            return False
        device_at = datetime.fromisoformat(str(data["updated_at"]))
        newer = lib.updated_at > device_at
        log.info(
            "position conflict for %s: device %.0f s at %s, library %.0f s at %s -> %s wins",
            ref.key(),
            float(data["position_s"]),
            device_at.isoformat(),
            lib.position_s,
            lib.updated_at.isoformat(),
            "library" if newer else "device",
        )
        return newer

    async def _send_play(
        self, sink: FeedbackSink, r: PendingRow, play: PlayEvent, out: _Outcome
    ) -> bool:
        """True: the play reached the library in this run (not a duplicate of an earlier one)."""
        name = f"{sink.sink_name}:{play.ref.library_id}"
        if await self.store.submitted_plays(name, [play]):
            await self.store.delivered(r.id)
            out.report.plays_duplicate += 1
            return False
        if r.inflight is not None:  # the last run died while sending this play: did it arrive?
            now = await sink.play_count(play.ref)
            if now is not None and now > r.inflight:
                await asyncio.shield(self.store.delivered(r.id, name, [play]))
                out.report.plays_submitted += 1
                return True
        await self.store.set_inflight(r.id, await sink.play_count(play.ref))
        result = await sink.submit_plays([play])
        if result.accepted:
            # shielded: a cancel right after the server took the play must not leave it unmarked
            await asyncio.shield(self.store.delivered(r.id, name, [play]))
            out.report.plays_submitted += 1
            return True
        elif result.rejected:
            raise _PlayRejected(result.rejected[0][1])
        else:
            raise _PlayFailed(result.failed[0][1] if result.failed else "not accepted by the sink")

    async def _failed(self, r: PendingRow, error: str, permanent: bool, out: _Outcome) -> None:
        if permanent and r.attempts + 1 >= MAX_ATTEMPTS:
            # kept with its reason for the UI's "Not delivered" list
            await self.store.drop_pending(r, out.device_id, error)
            msg = f"{_ref_of(r.kind, r.data).key()}: {r.kind} dropped after {MAX_ATTEMPTS} attempts: {error}"
            log.warning("%s", msg)
            out.report.errors.append(msg)
            out.report.dropped += 1
            return
        await self.store.retry_failed(r.id, error, count=permanent)
        out.errors.add(error)

    async def _record_scopes(self, device_id: str, libraries: Mapping[str, Library | None]) -> None:
        """After delivery: each position library's scopes as they are now, the reference for the next run. The forward sync that follows puts the library's positions that won on the device."""
        for lib_id, lib in libraries.items():
            if (
                lib is None
                or not isinstance(lib, FeedbackSink)
                or not lib.capabilities.supports_positions
            ):
                continue
            try:
                positions = await lib.scope_positions()
            except Exception as e:  # noqa: BLE001 - the old record stays; the next run tries again
                log.info("%s: positions of %s not recorded: %s", device_id, lib_id, _message(e))
                continue
            await self.store.set_position_scopes(
                device_id, lib_id, {k: p.position_s for k, p in positions.items()}
            )

    # --- consume ---------------------------------------------------------------------------------

    async def _commit(
        self, device: Device, manifest: Manifest, fb: DeviceFeedback, changed: bool = False
    ) -> None:
        """The device's data is consumed: manifest last-known values, then reset_feedback (same instance).
        `changed`: the manifest changed already (a new playlist base)."""
        plays_per_key: dict[str, int] = defaultdict(int)
        for p in fb.plays:
            plays_per_key[p.ref.key()] += 1
        for key, entry in manifest.items.items():
            if is_external(entry.ref.library_id):
                # not ours to send: kept for the installation whose library it is. Its rating and bookmark
                # baselines stay, so that installation still sees the device's change
                if plays_per_key.get(key):
                    entry.pending_plays += plays_per_key[key]
                    changed = True
                if key in fb.play_counts and fb.play_counts[key] != entry.last_playcount:
                    entry.last_playcount = fb.play_counts[key]
                    changed = True
                continue
            if entry.pending_plays:
                entry.pending_plays = 0  # captured by this run (_kept_plays)
                changed = True
            count = (
                fb.play_counts[key]
                if key in fb.play_counts
                else entry.last_playcount + plays_per_key.get(key, 0)
            )
            rating = fb.ratings.get(key, entry.last_rating)
            pos = fb.positions[key].position_s if key in fb.positions else entry.last_position_s
            if (count, rating, pos) != (
                entry.last_playcount,
                entry.last_rating,
                entry.last_position_s,
            ):
                entry.last_playcount, entry.last_rating, entry.last_position_s = count, rating, pos
                changed = True
        if not changed and fb.is_empty():
            return
        s = await device.begin()
        # only the manifest's baselines changed: a device that rewrites its own databases (HiBy) does that once, in
        # the forward sync's final commit
        s.data["manifest_only"] = True
        try:
            if changed:
                await device.commit(s, manifest)
            if not fb.is_empty():
                await device.reset_feedback(s, fb)
        except BaseException:
            await device.abort(s)
            raise
        if not changed:
            await device.abort(s)


def summary(r: ReverseReport) -> str:
    """The reverse sync in words for the log: "3 plays sent, 1 rating, 2 waiting" / "nothing new"."""

    def n(count: int, word: str) -> str:
        return f"{count} {word}{'' if count == 1 else 's'}"

    parts = []
    if r.plays_found:
        parts.append(f"{n(r.plays_found, 'play')} found, {r.plays_submitted} sent")
    if r.plays_duplicate:
        parts.append(f"{n(r.plays_duplicate, 'play')} already there")
    if r.ratings_pushed:
        parts.append(n(r.ratings_pushed, "rating"))
    if r.stars_pushed:
        parts.append(n(r.stars_pushed, "favourite"))
    if r.positions_pushed:
        parts.append(n(r.positions_pushed, "position"))
    if r.finished_pushed:
        parts.append(f"{r.finished_pushed} marked finished")
    if r.positions_superseded:
        parts.append(f"{n(r.positions_superseded, 'position')} newer in the library")
    if r.pending:
        parts.append(f"{r.pending} waiting for the library")
    if r.dropped:
        parts.append(f"{r.dropped} given up")
    if r.errors:
        parts.append(n(len(r.errors), "error"))
    return ", ".join(parts) or "nothing new"


__all__ = ["MAX_ATTEMPTS", "ReverseSync", "summary"]

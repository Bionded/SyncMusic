# SPDX-License-Identifier: GPL-3.0-or-later
"""Applier: carry out a SyncPlan on a device.

Order: removals (free space first) → adds/replaces → metadata updates → playlists → `device.commit()` (device
DB, then the manifest). Adds/replaces are prepared ahead by up to `workers` concurrent prepares (download +
processor chain, `core/prepare.py`) while writes to the device stay serial and in plan order.

Failure rules:
- one failing item (download, processing, write) is reported and skipped; the rest of the sync goes on and
  the item is planned again next time;
- space guard: before each put the free space must cover the file plus the commit reserve
  (`Device.commit_reserve`), otherwise the item is skipped for space; before the commit, if the reserve is not
  free (e.g. an iPod DB grew), the most recently added tracks are removed until it is;
- cancellation (`check_cancelled` raising) stops after the current file and commits what is done;
- checkpoints: while files are written, what is done is committed every `checkpoint_s` (at least ten
  times the last commit's duration, so a large iPod DB costs at most ~10 %); a crash or a lost device then loses only
  the files since the last checkpoint, and the next plan keeps everything committed before;
- a lost device (`check_device` raising `DeviceLostError`, checked before every write) fails the sync at once:
  nothing more is written, not even the manifest (the mountpoint may be a bare directory by then);
- a device error during commit (or anything unexpected) aborts the session and fails the sync - whatever was
  committed before is consistent; files written since are inflight and cleaned up by the next sync.
A replace removes the old file first, then puts the new one (file devices may reuse the path).
"""

from __future__ import annotations

import asyncio
import copy
import errno
import logging
import math
import sys
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from syncmusic import __version__
from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.base.device import Device, DeviceSession
from syncmusic.base.feedback import stores_ratings, stores_starred
from syncmusic.base.library import Library, library_unavailable
from syncmusic.base.progress import on_progress
from syncmusic.core.models import (
    ActionKind,
    DeviceTrackRef,
    Interruption,
    ItemFailure,
    Manifest,
    ManifestEntry,
    ManifestHeader,
    MediaKind,
    PlanAction,
    SyncPlan,
    SyncReport,
    SyncSettings,
    Track,
    WorkItem,
    playlist_hash,
    utcnow,
)
from syncmusic.core.planner import meta_snapshot, position_changed
from syncmusic.core.prepare import (
    CAPS_HASH_VERSION,
    Chain,
    NoCache,
    Preparer,
    StepError,
    memory_estimate,
)

log = logging.getLogger(__name__)

CHECKPOINT_S = 60.0  # seconds of writing between two checkpoint commits
COPY_YIELD_EVERY = 100  # deep-copied manifest entries between two yields (slow per item)
YIELD_EVERY = 500  # loop items between two yields to the event loop in long in-memory passes
# a library that does not answer a download pauses for these delays, one per failed retry, then its remaining
# items are postponed to the next sync
RETRY_DELAYS_S = (5.0, 15.0, 60.0, 120.0, 180.0)


class Cancelled(Exception):
    """Raised by a `check_cancelled` callback (any exception named *Cancelled* is treated the same)."""


class DeviceLostError(Exception):
    """Raised by a `check_device` callback: the device's mount is gone, nothing more may be written."""


class DeviceIOError(OSError):
    """The device's storage failed a read or write (EIO, or the kernel remounted it read-only after errors): the
    sync stops at once instead of trying every other file (each failing write can hang for minutes)."""


def _device_io_error(
    device_id: str, e: BaseException, item: str = "", space_checked: bool = False
) -> DeviceIOError | None:
    """EIO/EROFS; also ENOSPC after the space guard said the file fits (`space_checked`): then the storage under
    the file system ran out (a lying card, a full loop backing store), not the file system - without vfat's `flush`
    the kernel reports such a write-back failure as ENOSPC at fsync."""
    codes = (errno.EIO, errno.EROFS, *((errno.ENOSPC,) if space_checked else ()))
    if isinstance(e, OSError) and e.errno in codes:
        during = f" while writing {item}" if item else ""
        return DeviceIOError(
            e.errno,
            f"{device_id}: the device's storage reports {e.strerror or 'an I/O error'}{during}; it may be "
            "failing (check the kernel log, run a file system check or replace the storage)",
        )
    return None


def storage_failure(e: BaseException) -> bool:
    """The sync stopped because the device's storage failed or it dropped off (an I/O error, the mount vanished):
    the kind of failure gentle write mode may help with."""
    seen: set[int] = set()
    cur: BaseException | None = e
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, (DeviceIOError, DeviceLostError)):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False


def _is_cancel(e: BaseException) -> bool:
    return type(e).__name__.endswith("Cancelled") and not isinstance(e, asyncio.CancelledError)


def describe(a: PlanAction) -> str:
    """The item as a person knows it: "artist – album – title" (a podcast: show – episode, a book:
    author – book – chapter); without library data the plan's title."""
    t = a.item.track if a.item is not None else None
    if t is None:
        return a.title or a.ref.key()
    parts = (t.album_artist or t.artist, t.album, t.title)
    return " – ".join(p for p in parts if p) or a.title or a.ref.key()


def track_ref(e: ManifestEntry) -> DeviceTrackRef:
    return DeviceTrackRef(
        device_ref=e.device_ref, path=e.path, size=e.size, sidecars=list(e.sidecars)
    )


async def _device_ok() -> None:
    return None


def outcome_summary(report: SyncReport) -> str:
    """One-line outcome for the job message, e.g. 'partial: 22 synced, 3 skipped for space, 0 failed'."""
    c = report.counts
    synced = c.get("add", 0) + c.get("replace", 0)
    word = "done" if report.status == "succeeded" else report.status
    parts = [
        f"{synced} synced",
        f"{len(report.skipped_for_space)} skipped for space",
        f"{len(report.failures)} failed",
    ]
    if report.postponed:
        parts.append(f"{report.postponed} postponed")
    if report.removed_for_commit:
        parts.append(f"{len(report.removed_for_commit)} removed to fit")
    return f"{word}: {', '.join(parts)}"


class WritePacer:
    """Gentle write mode: after every `batch` written files the file system is synced and the
    writer pauses; with `rate` (bytes/s) every file waits until the average write rate is back under it."""

    def __init__(
        self,
        batch: int,
        pause_s: float,
        rate: float | None,
        flush: Callable[[], Awaitable[None]],
        *,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.batch, self.pause_s, self.rate = batch, pause_s, rate
        self.flush, self.sleep, self.clock = flush, sleep, clock
        self.files = self.bytes = 0
        self.start: float | None = None

    async def after_file(self, size: int) -> None:
        if self.start is None:
            self.start = self.clock()
        self.files += 1
        self.bytes += size
        if self.rate:
            ahead = self.bytes / self.rate - (self.clock() - self.start)
            if ahead > 0:
                await self.sleep(ahead)
        if self.files % self.batch == 0:
            await self.flush()
            await self.sleep(self.pause_s)


async def _syncfs(path: Path) -> None:
    """Write the file system holding `path` back to the disk (`sync -f`)."""
    proc = await asyncio.create_subprocess_exec(
        "sync", "-f", str(path), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    if proc.returncode:
        log.warning("sync -f %s: %s", path, err.decode(errors="replace").strip())


async def _no_flush() -> None:
    return None


def gentle_pacer(
    settings: SyncSettings, mount: Path, limit_mb_s: float | None = None
) -> WritePacer | None:
    """The pacer of a device in gentle write mode; `limit_mb_s` is the app-wide write speed limit (Settings → Sync defaults) for every disk device, the lower one wins in gentle mode. None: write at full speed."""
    limits = [
        x for x in (limit_mb_s, settings.gentle_rate_mb_s if settings.gentle_write else None) if x
    ]
    rate = min(limits) * 1_000_000 if limits else None
    if settings.gentle_write:
        return WritePacer(
            settings.gentle_batch_files, settings.gentle_pause_s, rate, lambda: _syncfs(mount)
        )
    return WritePacer(sys.maxsize, 0.0, rate, _no_flush) if rate else None


STATUS_EVERY_S = 2.0  # how often a writer waiting for a long prepare reports what it waits for


@dataclass
class _Slot:
    """One add/replace being prepared ahead of its write."""

    action: PlanAction
    ready: asyncio.Future[WorkItem]
    written: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


class _Outage:
    """Libraries whose downloads fail with a connection error: each failure pauses the library for the
    next RETRY_DELAYS_S delay; after the last one it is given up and its items are postponed."""

    def __init__(self) -> None:
        self._paused: dict[
            str, float
        ] = {}  # library id -> monotonic time its downloads are tried again
        self._tries: dict[str, int] = {}
        self._postponed: dict[str, int] = {}  # given-up library id -> items postponed
        self._probing: set[str] = set()  # retried libraries with their one probe download in flight
        self.reason = ""

    def next_ready(self, todo: deque[PlanAction]) -> PlanAction | None:
        """Take the first item whose library is not paused out of `todo`; a library being retried gets one
        download at a time until it answers."""
        now = time.monotonic()
        for i, a in enumerate(todo):
            lib = a.ref.library_id
            if self._paused.get(lib, 0.0) > now or lib in self._probing:
                continue
            if self._tries.get(lib):
                self._probing.add(lib)
            del todo[i]
            return a
        return None

    def resume_at(self, todo: deque[PlanAction]) -> float:
        return min(self._paused.get(a.ref.library_id, 0.0) for a in todo)

    def failed(self, library_id: str, e: BaseException, name: str) -> bool:
        """A download of `library_id` could not reach it. True: retry the item after the pause; False: the
        library is given up, postpone its items."""
        self._probing.discard(library_id)
        if library_id in self._postponed:
            return False
        now = time.monotonic()
        if self._paused.get(library_id, 0.0) > now:
            return True  # in flight when the library was paused: no new attempt counted
        n = self._tries.get(library_id, 0)
        if n >= len(RETRY_DELAYS_S):
            log.warning("%s still unreachable after %d retries: its items are postponed", name, n)
            self._postponed[library_id] = 0
            self._paused.pop(library_id, None)
            return False
        self._tries[library_id] = n + 1
        self._paused[library_id] = now + RETRY_DELAYS_S[n]
        self.reason = f"{name} unreachable"
        log.warning(
            "%s unreachable (%s): its downloads pause for %.0f s (retry %d of %d)",
            name,
            e,
            RETRY_DELAYS_S[n],
            n + 1,
            len(RETRY_DELAYS_S),
        )
        return True

    def answered(self, library_id: str) -> None:
        """A download of `library_id` worked: it is back."""
        self._tries.pop(library_id, None)
        self._probing.discard(library_id)

    def count(self, library_id: str) -> None:
        self._postponed[library_id] = self._postponed.get(library_id, 0) + 1

    def postponed(self) -> dict[str, int]:
        return {k: v for k, v in self._postponed.items() if v}


class Applier:
    def __init__(
        self,
        device: Device,
        libraries: Mapping[str, Library],
        chain: Chain,
        preparer: Preparer,
        *,
        workers: int = 2,
        check_cancelled: Callable[[], None] | None = None,
        # (fraction or None: unchanged, message, **stats: phase / files_done / files_total / bytes / failed)
        progress: Callable[..., None] | None = None,
        check_device: Callable[[], Awaitable[None]] | None = None,
        device_positions: Collection[str] | None = None,
        checkpoint_s: float | None = CHECKPOINT_S,
        pacer: WritePacer | None = None,
        no_cache: NoCache | None = None,
        reverse_ok: bool = True,
    ) -> None:
        self.device = device
        self.libraries = libraries
        self.chain = chain
        self.preparer = preparer
        self.workers = max(1, workers)
        # no-cache mode: the window is bounded by the held files and their memory budget instead
        self.no_cache = no_cache
        self.check_cancelled = check_cancelled or (lambda: None)
        self.progress = progress or (lambda f, m, **kw: None)
        # raises DeviceLostError when the device root is no longer the live mount;
        # called before every write so nothing lands in the bare mountpoint directory
        self.check_device = check_device or _device_ok
        # keys whose bookmark moved on the device (step 0); None: the device's positions are unknown, so bookmarks
        # are not moved to the library's position at all
        self.device_positions = device_positions
        self.checkpoint_s = checkpoint_s  # None: commit only at the end
        self._next_checkpoint = 0.0  # time.monotonic() of the next checkpoint
        self.pacer = pacer  # gentle write mode
        # the reverse sync before this job read the device and pushed its data: a device that rewrites its own
        # databases from the libraries at commit (HiBy) takes the libraries' values
        self.reverse_ok = reverse_ok
        self._caps: DeviceCapabilities | None = None

    def caps(self) -> DeviceCapabilities:
        """The device's effective capabilities, resolved once per sync after the session opened (resolving them per item cost ~9 % of a first sync's writer time)."""
        if self._caps is None:
            self._caps = self.device.effective_capabilities()
        return self._caps

    async def apply(self, plan: SyncPlan, manifest: Manifest | None = None) -> SyncReport:
        """Apply `plan`. `manifest` replaces the device's as the starting point (first sync "adopt")."""
        report = SyncReport(id=uuid.uuid4().hex, device_id=self.device.id, plan_id=plan.id)
        report.skipped_for_space = list(plan.space.dropped)
        s = await self.device.begin()
        self._caps = None  # the open session may know the model better (iPod generation)
        s.data["reverse_ok"] = self.reverse_ok
        s.data["device_positions"] = set(self.device_positions or ())
        s.data["synced_kinds"] = plan.synced_kinds  # None: every kind is synced
        if manifest is not None:
            s.manifest = manifest
        manifest = await self._start_manifest(s)
        manifest.header.processing_inputs.update(plan.processing_inputs)  # what the hashes mean
        # ratings / positions / playlists: what changed besides files, for the update rows
        counts = dict.fromkeys(
            (
                "add",
                "replace",
                "update_meta",
                "remove",
                "keep",
                "ratings",
                "positions",
                "playlists",
            ),
            0,
        )
        added: list[str] = []  # keys added/replaced in this run, in order
        cancelled = False
        try:
            try:
                await self._removals(s, plan, manifest, report, counts)
                await self._puts(s, plan, manifest, report, counts, added)
            except Exception as e:
                if not _is_cancel(e):
                    raise
                cancelled = True
                log.info("sync of %s cancelled: committing what is done", self.device.id)
            if not cancelled:
                self.progress(None, "writing the database", phase="database", preparing=[])
                await self._metadata(s, plan, manifest, report, counts)
                await self.check_device()
                await self._playlists(s, plan, manifest, counts, report)
            await self._fit_commit(s, manifest, report, added)
            await self.check_device()
            await self._commit(s, manifest)
            # what the device could not write at commit (e.g. a HiBy database) shows in the report
            report.warnings += [
                w for w in s.data.pop("commit_warnings", []) if w not in report.warnings
            ]
            report.player_notice = s.data.pop("player_notice", "") or report.player_notice
            # what the device wrote into its own databases (HiBy: tracks / hearts / playlists)
            counts.update(s.data.pop("commit_counts", {}))
        except BaseException as e:
            report.status = "failed"
            report.message = f"{type(e).__name__}: {e}"
            report.finished_at = utcnow()
            if isinstance(e, DeviceIOError):
                # deleting this run's files would hit the failing storage again; the next sync cleans them up
                log.error("%s", e)
                raise
            try:
                await self.device.abort(s)
            except Exception:  # noqa: BLE001 - the original error matters
                log.exception("abort after failed sync of %s", self.device.id)
            raise
        counts["keep"] = plan.count(ActionKind.KEEP)
        report.counts = counts
        report.finished_at = utcnow()
        if cancelled:
            report.status = "cancelled"
        elif (
            report.failures
            or report.postponed
            or report.removed_for_commit
            or len(report.skipped_for_space) > len(plan.space.dropped)
        ):
            report.status = "partial"
        else:
            report.status = "succeeded"
        if report.status == "succeeded" and not report.skipped_for_space:
            self.progress(1.0, "done")
        else:
            report.message = outcome_summary(report)
            # a cancelled run keeps the fraction it reached; a partial one went through everything
            self.progress(None if cancelled else 1.0, report.message)
        return report

    async def _commit(
        self, s: DeviceSession, manifest: Manifest, interrupted: Interruption | None = None
    ) -> None:
        manifest.header.interrupted = interrupted
        used = {h for e in manifest.items.values() for h in e.processing.values()}
        inputs = manifest.header.processing_inputs
        manifest.header.processing_inputs = {h: v for h, v in inputs.items() if h in used}
        manifest.header.updated_at = utcnow()
        manifest.header.app_version = __version__

        def indexing(done: int, total: int | None) -> None:
            # a device that indexes its files at commit (HiBy) says how far it got, so it never looks hung
            msg = f"indexing {done:,} of {total:,}" if total else "indexing"
            self.progress(None, msg, phase="database")

        with on_progress(indexing):
            await self.device.commit(s, manifest)

    async def _checkpoint(
        self,
        s: DeviceSession,
        manifest: Manifest,
        plan: SyncPlan,
        done: int,
        total: int,
        report: SyncReport,
    ) -> None:
        """Commit what is done so far when the checkpoint is due; the session stays open. The header
        says how far this sync got until the final commit clears it."""
        if self.checkpoint_s is None or time.monotonic() < self._next_checkpoint:
            return
        await self.check_device()
        started = time.monotonic()
        await self._commit(s, manifest, Interruption(plan_id=plan.id, done=done, total=total))
        took = time.monotonic() - started
        self._next_checkpoint = time.monotonic() + max(self.checkpoint_s, 10 * took)
        log.info(
            "%s: checkpoint, %d items committed (%.1f s); so far writing %.0f s, waiting for prepares %.0f s",
            self.device.id,
            len(manifest.items),
            took,
            report.write_seconds,
            report.wait_seconds,
        )

    def _fail(
        self,
        report: SyncReport,
        a: PlanAction,
        step: str,
        e: BaseException,
        error: str,
        path: str = "",
    ) -> None:
        """Report and log a failed item with the track it was, the step and the file on the device."""
        item = describe(a)
        report.failures.append(
            ItemFailure(
                ref=a.ref.key(), title=a.title, error=error, item=item, step=step, path=path
            )
        )
        log.warning(
            "%s: %s failed for %s (%s%s): %s",
            self.device.id,
            step,
            item,
            a.ref.key(),
            f", {path}" if path else "",
            e,
            extra={"kv": {"item": item, "item_step": step, "ref": a.ref.key(), "path": path}},
        )

    def _forget_lost(self, a: PlanAction, manifest: Manifest) -> None:
        """An `add` of an item still in the manifest means its file is gone from the device (removed by hand); when
        the add does not happen, the stale entry goes too - kept, the verification would report the item again
        and turn the sync `failed`. The next plan adds it again."""
        if a.kind is ActionKind.ADD and manifest.items.pop(a.ref.key(), None) is not None:
            log.info(
                "%s: %s is not on the device: its manifest entry is dropped",
                self.device.id,
                describe(a),
            )

    # --- steps ------------------------------------------------------------------------------------

    async def _start_manifest(self, s: DeviceSession) -> Manifest:
        uid = self.device.config.identity.manifest_device_id
        if s.manifest is not None:
            # the applier changes entries in place: a deep copy. One deepcopy of a big manifest in a thread
            # starves the event loop through the GIL just the same; copying entry by entry with yields keeps
            # every slice short
            src = s.manifest
            items: dict[str, ManifestEntry] = {}
            for n, (k, e) in enumerate(src.items.items()):
                if n % COPY_YIELD_EVERY == COPY_YIELD_EVERY - 1:
                    await asyncio.sleep(0)
                items[k] = e.model_copy(deep=True)
            rest = {
                f: copy.deepcopy(getattr(src, f)) for f in Manifest.model_fields if f != "items"
            }
            m = Manifest.model_construct(_fields_set=src.model_fields_set, items=items, **rest)
            m.header.device_id, m.header.device_uid = self.device.id, uid  # taken over if foreign
            return m
        return Manifest(
            header=ManifestHeader(device_id=self.device.id, device_uid=uid, app_version=__version__)
        )

    async def _removals(
        self,
        s: DeviceSession,
        plan: SyncPlan,
        manifest: Manifest,
        report: SyncReport,
        counts: dict[str, int],
    ) -> None:
        if any(a.kind is ActionKind.REMOVE and a.entry is not None for a in plan.actions):
            # removals are file work too: their log lines carry the copy step, not the one before
            self.progress(None, None, phase="copy")
        for a in plan.actions:
            if a.kind is not ActionKind.REMOVE or a.entry is None:
                continue
            self.check_cancelled()
            await self.check_device()
            try:
                await self.device.remove_track(s, track_ref(a.entry))
            except Exception as e:  # noqa: BLE001 - reported, retried next sync
                self._fail(report, a, "remove", e, f"remove: {e}", a.entry.path)
                continue
            manifest.items.pop(a.ref.key(), None)
            counts["remove"] += 1

    async def _prepare(self, slot: _Slot) -> None:
        a = slot.action
        assert a.item is not None
        try:
            library = self.libraries[a.ref.library_id]
            if a.tags_only and a.entry is not None:  # from the device's file, no transcode
                ref = track_ref(a.entry)
                async with self.preparer.retagged(
                    library,
                    a.item,
                    self.chain,
                    self.caps(),
                    lambda dest: self.device.fetch_track(ref, dest),
                    self.check_cancelled,
                ) as retagged:
                    if retagged is not None:
                        slot.ready.set_result(retagged)
                        await slot.written.wait()
                        return
            async with self.preparer.prepared(
                library,
                a.item,
                self.chain,
                self.caps(),
                self.check_cancelled,
                self.no_cache,
            ) as work:
                slot.ready.set_result(work)
                await slot.written.wait()  # keep the files until the device write is done
        except asyncio.CancelledError:
            slot.ready.cancel()  # no-op when already set
            raise
        except BaseException as e:
            if not slot.ready.done():
                slot.ready.set_exception(e)
            else:
                log.warning("cleanup after %s failed: %s", describe(a), e)

    async def _puts(
        self,
        s: DeviceSession,
        plan: SyncPlan,
        manifest: Manifest,
        report: SyncReport,
        counts: dict[str, int],
        added: list[str],
    ) -> None:
        todo = deque(a for a in plan.actions if a.kind in (ActionKind.ADD, ActionKind.REPLACE))
        outage = _Outage()  # libraries that stopped answering downloads
        # progress by the planned bytes (transcode + copy time follows the size, the ETA uses it)
        weight = {id(a): max(1, a.item.file.size if a.item else 0) for a in todo}
        total = sum(weight.values()) or 1
        window: deque[_Slot] = deque()
        loop = asyncio.get_running_loop()

        def held(
            a: PlanAction,
        ) -> int:  # memory an item takes while it waits for its write (no-cache mode)
            nc = self.no_cache
            return memory_estimate(a.item) if nc and a.item and nc.in_memory(a.item) else 0

        def room() -> bool:
            if not todo:
                return False
            if self.no_cache is None:
                return len(window) < self.workers
            if not window:
                return True
            in_mem = sum(held(s.action) for s in window)
            return (
                len(window) < self.no_cache.files
                and in_mem + held(todo[0]) <= self.no_cache.budget_bytes
            )

        def launch() -> None:
            while room():
                a = outage.next_ready(todo)
                if a is None:  # only items of paused libraries left
                    return
                slot = _Slot(a, loop.create_future())
                slot.task = asyncio.create_task(self._prepare(slot))
                window.append(slot)

        done = files = 0
        if self.checkpoint_s is not None:
            self._next_checkpoint = time.monotonic() + self.checkpoint_s
        if todo:
            # before the first item: what it logs (and its prepare task, which copies the log context when
            # launched) is the copy step, not the step before
            self.progress(None, None, phase="copy", files_done=0, files_total=len(weight))
        started = time.monotonic()

        def postpone(a: PlanAction) -> None:
            nonlocal done
            report.postponed += 1
            outage.count(a.ref.library_id)
            self._forget_lost(a, manifest)
            done += weight[id(a)]

        try:
            launch()
            while window or todo:
                if not window:  # every item left waits for a library to answer again
                    await self._wait_for_library(outage, todo)
                    launch()
                    continue
                slot = window[0]
                a = slot.action
                waited = time.monotonic()
                logged = False
                try:
                    while True:
                        try:
                            work = await asyncio.wait_for(
                                asyncio.shield(slot.ready), STATUS_EVERY_S
                            )
                            break
                        except TimeoutError:  # a long prepare: say what it waits for
                            if msg := self._merge_status(window):
                                # the merged share of this item moves the bar and the ETA; only the first
                                # status goes to the job log, the rest just update the Now line
                                merge = self.preparer.merging.get(a.ref.key())
                                share = merge.fraction() if merge is not None else 0.0
                                self.progress(
                                    (done + weight[id(a)] * share) / total,
                                    msg,
                                    log=not logged,
                                    phase="copy",
                                    files_done=files,
                                    files_total=len(weight),
                                    preparing=self._preparing(window),
                                )
                                logged = True
                except Exception as e:
                    if _is_cancel(e):
                        raise
                    window.popleft()
                    step = e.step if isinstance(e, StepError) else "prepare"
                    if step == "download" and library_unavailable(e):
                        # the library is down, not the item: retried after a pause, postponed if it stays down
                        lib = a.ref.library_id
                        if outage.failed(lib, e, self._library_name(lib)):
                            todo.appendleft(a)
                        else:
                            postpone(a)
                            for b in [b for b in todo if b.ref.library_id == lib]:
                                todo.remove(b)
                                postpone(b)
                        launch()
                        continue
                    self._fail(report, a, step, e, str(e))
                    self._forget_lost(a, manifest)
                    launch()
                    continue
                finally:
                    report.wait_seconds += time.monotonic() - waited
                outage.answered(a.ref.library_id)
                await self.check_device()
                written = report.bytes_written
                try:
                    await self._put_one(s, a, work, manifest, report, counts, added)
                except Exception as e:
                    if _is_cancel(e):
                        raise
                    self._forget_lost(a, manifest)
                    if (io := _device_io_error(self.device.id, e, describe(a), True)) is not None:
                        self._fail(report, a, "put", e, str(e))
                        raise io from e
                    self._fail(report, a, "put", e, str(e))
                finally:
                    slot.written.set()
                    window.popleft()
                done += weight[id(a)]
                files += 1
                self.progress(
                    done / total,
                    a.title,
                    phase="copy",
                    files_done=files,
                    files_total=len(weight),
                    bytes=report.bytes_written,
                    failed=len(report.failures),
                    preparing=self._preparing(window),
                )
                if self.pacer is not None and report.bytes_written > written:
                    await self.pacer.after_file(report.bytes_written - written)
                self.check_cancelled()  # stop after the current file
                launch()
                await self._checkpoint(s, manifest, plan, files, len(weight), report)
            for lib, n in outage.postponed().items():
                report.warnings.append(
                    f"{self._library_name(lib)} unreachable - {n} items postponed to the next sync"
                )
        finally:
            if files:
                took = time.monotonic() - started
                log.info(
                    "%s: copied %d files, %.0f MB in %.0f s (%.1f MB/s): writing %.0f s, waiting for "
                    "prepares %.0f s",
                    self.device.id,
                    files,
                    report.bytes_written / 1e6,
                    took,
                    report.bytes_written / 1e6 / max(took, 1e-3),
                    report.write_seconds,
                    report.wait_seconds,
                )
            for slot in window:
                slot.written.set()
                if slot.task is not None:
                    slot.task.cancel()
            await asyncio.gather(*(sl.task for sl in window if sl.task), return_exceptions=True)

    def _merge_status(self, window: deque[_Slot]) -> str | None:
        """The audiobook merges of this sync's prepares in flight, for the status line."""
        merging = self.preparer.merging
        found = [m.status() for sl in window if (m := merging.get(sl.action.ref.key())) is not None]
        return "; ".join(found) or None

    def _preparing(self, window: deque[_Slot]) -> list[str]:
        """What the prepares in flight do, for the sync page: each audiobook merge's status,
        then the conversions grouped by format ('Converting 3 FLAC → ALAC') and the other files."""
        merging = self.preparer.merging
        lines: list[str] = []
        convert: dict[tuple[str, str], int] = {}
        other = 0
        for sl in window:
            if sl.ready.done():
                continue
            if (m := merging.get(sl.action.ref.key())) is not None:
                lines.append(m.status())
            elif (
                (it := sl.action.item) is not None
                and it.track.format
                and it.track.format != it.file.format
            ):
                pair = (it.track.format.upper(), it.file.format.upper())
                convert[pair] = convert.get(pair, 0) + 1
            else:
                other += 1
        lines += [f"Converting {n} {src} → {dst}" for (src, dst), n in convert.items()]
        if other:
            lines.append(f"Preparing {other} file" + ("" if other == 1 else "s"))
        return lines

    def _library_name(self, library_id: str) -> str:
        lib = self.libraries.get(library_id)
        return lib.config.name if lib is not None else library_id

    async def _wait_for_library(self, outage: _Outage, todo: deque[PlanAction]) -> None:
        """Nothing to prepare until a paused library is tried again: wait for it, cancellable."""
        until = outage.resume_at(todo)
        while (left := until - time.monotonic()) > 0:
            self.progress(None, f"{outage.reason}: retrying in {math.ceil(left)} s")
            self.check_cancelled()
            await asyncio.sleep(min(1.0, left))

    async def _put_one(
        self,
        s: DeviceSession,
        a: PlanAction,
        work: WorkItem,
        manifest: Manifest,
        report: SyncReport,
        counts: dict[str, int],
        added: list[str],
    ) -> None:
        key = a.ref.key()
        size = work.path.stat().st_size + sum(p.stat().st_size for p in work.sidecars.values())
        old = manifest.items.get(key) if a.kind is ActionKind.REPLACE else None
        freed = old.size if old is not None else 0
        free = await self.device.free_space()
        reserve = self.device.commit_reserve(len(manifest.items) + (0 if old else 1), self.caps())
        if free + freed - size < reserve:
            report.skipped_for_space.append(key)
            self._forget_lost(a, manifest)
            return
        patched = None
        if old is not None and a.tags_only:  # in place where the device can
            started = time.monotonic()
            patched = await self.device.rewrite_tags(s, track_ref(old), work)
            report.write_seconds += time.monotonic() - started
            if patched is not None:
                size = sum(p.stat().st_size for p in work.sidecars.values())
        if old is not None and patched is None:
            await self.device.remove_track(s, track_ref(old))
            manifest.items.pop(key, None)
            if not stores_ratings(self.libraries.get(a.ref.library_id)):
                # the new file keeps the device's rating: the library cannot hold it
                track = work.track.model_copy(update={"rating": old.last_rating})
                work = work.model_copy(
                    update={"plan": work.plan.model_copy(update={"track": track})}
                )
        if patched is not None:
            ref = patched
        else:
            started = time.monotonic()
            ref = await self.device.put_track(s, work)
            report.write_seconds += time.monotonic() - started
        t = work.track
        manifest.items[key] = ManifestEntry(
            ref=a.ref,
            device_ref=ref.device_ref,
            path=ref.path,
            size=ref.size,
            source_state=t.source_state,
            chain_hash=a.chain_hash,
            processing=a.processing,
            caps_v=CAPS_HASH_VERSION,
            meta_hash=a.meta_hash,
            meta=meta_snapshot(t),
            rules=list(a.rules),
            kind=t.kind,
            duration_s=t.duration_s,
            sidecars=list(ref.sidecars),
            layout=self.caps().layout_for(t.kind, t.disc_total),
            last_playcount=ref.stats.playcount if ref.stats else t.play_count,
            last_rating=ref.stats.rating if ref.stats else t.rating,
            # the bookmark written from the library is not a position made on the device
            last_position_s=ref.stats.position_s if ref.stats else t.position_s,
        )
        report.bytes_written += size
        counts["replace" if a.kind is ActionKind.REPLACE else "add"] += 1
        added.append(key)

    async def _metadata(
        self,
        s: DeviceSession,
        plan: SyncPlan,
        manifest: Manifest,
        report: SyncReport,
        counts: dict[str, int],
    ) -> None:
        writes_ratings = self.caps().supports_ratings_write
        # the library's favourites of every item synced, for devices with hearts (HiBy)
        starred: dict[str, bool] = s.data.setdefault("library_starred", {})
        tracks: dict[str, Track] = s.data.setdefault("library_tracks", {})
        for n, a in enumerate(plan.actions):
            if n % YIELD_EVERY == YIELD_EVERY - 1:  # thousands of keeps: serve the UI in between
                await asyncio.sleep(0)
            if a.item is not None:
                tracks[a.ref.key()] = a.item.track
            if a.item is not None and stores_starred(self.libraries.get(a.ref.library_id)):
                starred[a.ref.key()] = a.item.track.starred
            entry = manifest.items.get(a.ref.key())
            if entry is not None and a.kind is ActionKind.KEEP:
                entry.orphan = a.orphan  # gone from its library; back when the library has it again
            if entry is None or a.item is None:
                continue
            track = a.item.track
            if not stores_ratings(self.libraries.get(a.ref.library_id)):
                # the library cannot hold the device's rating: keep the device's own
                track = track.model_copy(update={"rating": entry.last_rating})
            if a.kind is ActionKind.UPDATE_META:
                await self.check_device()
                try:
                    await self.device.update_metadata(s, track_ref(entry), track)
                except Exception as e:  # noqa: BLE001
                    if (io := _device_io_error(self.device.id, e, describe(a))) is not None:
                        self._fail(report, a, "metadata", e, f"metadata: {e}", entry.path)
                        raise io from e
                    self._fail(report, a, "metadata", e, f"metadata: {e}", entry.path)
                    continue
                counts["update_meta"] += 1
                if writes_ratings:  # the device now holds this rating (else it keeps its own; 8.4)
                    if track.rating != entry.last_rating:
                        counts["ratings"] += 1
                    entry.last_rating = track.rating
            if a.kind in (ActionKind.UPDATE_META, ActionKind.KEEP):
                if a.kind is ActionKind.UPDATE_META or not entry.meta:
                    entry.meta = meta_snapshot(a.item.track)  # what the device holds now
                entry.meta_hash = a.meta_hash or entry.meta_hash
                if (
                    a.restamp
                ):  # the source was only touched: the file on the device is still current
                    entry.source_state = a.item.track.source_state
                if a.rehash:  # only the caps hash scheme changed: the file stays
                    entry.chain_hash, entry.processing = a.chain_hash, a.processing
                if a.processing and entry.chain_hash == a.chain_hash:
                    entry.processing = a.processing  # entries written before
                    entry.caps_v = CAPS_HASH_VERSION
                entry.rules = list(a.rules) or entry.rules
                if await self._position(s, a, entry, report):
                    counts["positions"] += 1

    async def _position(
        self, s: DeviceSession, a: PlanAction, entry: ManifestEntry, report: SyncReport
    ) -> bool:
        """A podcast/audiobook listened further elsewhere: move the device's bookmark to the library's position,
        unless the device moved it too (its change was pushed and wins)."""
        pos = a.item.track.position_s if a.item else None
        if (
            self.device_positions is None
            or entry.kind is MediaKind.MUSIC
            or pos is None
            or a.ref.key() in self.device_positions
            or not position_changed(pos, entry.last_position_s)  # none == 0:00
        ):
            return False
        await self.check_device()
        try:
            finished = a.item.track.finished if a.item else False
            if await self.device.set_position(s, track_ref(entry), pos, finished):
                entry.last_position_s = pos
                return True
        except Exception as e:  # noqa: BLE001
            self._fail(report, a, "position", e, f"position: {e}", entry.path)
        return False

    async def _playlists(
        self,
        s: DeviceSession,
        plan: SyncPlan,
        manifest: Manifest,
        counts: dict[str, int],
        report: SyncReport,
    ) -> None:
        if not self.caps().supports_playlists:
            return
        on_device = set(manifest.items)
        playlists = [
            p.model_copy(update={"refs": [r for r in p.refs if r.key() in on_device]})
            for p in plan.playlists
        ]
        # device-made playlists already in a library that no rule selects leave the device; one
        # whose push failed is not in `playlist_created`, so it stays for the next sync
        selected = {p.name for p in playlists}
        gone = [n for n in manifest.playlist_created if n not in selected]
        if gone:
            await self.device.remove_playlists(s, gone)
            for n in gone:
                del manifest.playlist_created[n]
                log.info(
                    "%s: device playlist %r is in the library: removed from the device",
                    self.device.id,
                    n,
                )
            report.playlists_removed = gone
        await self.device.set_playlists(s, playlists)
        hashes = {p.name: playlist_hash(p.refs) for p in playlists}
        # changed = new, or its tracks differ; one written before hashes were stored is not counted
        smart = {p.name for p in playlists if p.smart}

        def same_songs(n: str) -> bool:
            # a smart playlist's order may differ on every evaluation (ties, random sort): rewritten, but only a
            # change of its songs is logged
            before = manifest.playlist_refs.get(n)
            return before is not None and set(before) == {r.key() for r in by_name[n].refs}

        by_name = {p.name: p for p in playlists}
        changed = [
            n
            for n, h in hashes.items()
            if n not in manifest.playlists
            or (manifest.playlist_hashes.get(n, h) != h and not (n in smart and same_songs(n)))
        ]
        counts["playlists"] += len(changed)
        sizes = {p.name: len(p.refs) for p in playlists}
        report.playlists_written = {n: sizes[n] for n in changed}  # the activity log
        report.playlists_dropped = [n for n in manifest.playlists if n not in hashes]
        manifest.playlists = [p.name for p in playlists]
        manifest.playlist_hashes = hashes
        # the merge base of the reverse playlist sync
        written = {p.name: [r.key() for r in p.refs] for p in playlists}
        # a device-made playlist the rules now select is managed from here on; the others keep their base
        manifest.playlist_created = {
            n: s for n, s in manifest.playlist_created.items() if n not in written
        }
        manifest.playlist_refs = {
            n: r for n, r in manifest.playlist_refs.items() if n in manifest.playlist_created
        } | written
        manifest.playlist_sources = {p.name: p.source.key() for p in playlists if p.source}
        manifest.playlist_smart = {p.name for p in playlists if p.smart}

    async def _fit_commit(
        self, s: DeviceSession, manifest: Manifest, report: SyncReport, added: list[str]
    ) -> None:
        """Remove the most recently added tracks until the commit reserve is free."""
        while added and await self.device.free_space() < self.device.commit_reserve(
            len(manifest.items)
        ):
            key = added.pop()
            entry = manifest.items.pop(key)
            await self.check_device()
            await self.device.remove_track(s, track_ref(entry))
            report.removed_for_commit.append(key)
            log.warning("removed %s again: not enough space to commit", entry.path)

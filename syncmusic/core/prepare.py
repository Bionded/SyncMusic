# SPDX-License-Identifier: GPL-3.0-or-later
"""Prepare step: original → processor chain → cache → file ready for the device.

- `Chain` holds a device's enabled processors in order. `Chain.plan()` folds every `plan()` (pure);
  `Chain.cache_key()` / `Chain.chain_hash()` identify the output for the cache and the device manifest.
- `Preparer.prepared()` is an async context manager yielding the finished `WorkItem`:
  cache hit → files straight from the cache (no processing); miss → fetch the original (local path as is,
  remote stream downloaded once into the cache), fetch the cover/lyrics the chain `needs`, run the chain in a
  fresh work dir, store the result in the cache. The cache entry is pinned while the context is open and the
  work dir is always removed afterwards (also on error or cancellation).
- `Preparer.retagged()`: a tags-only processing change starts from the file already on the device
  and runs only the processors that are not of the audio stage - no download, no transcode, no cache.
- No-cache mode (`NoCache`): nothing is stored in the cache (an existing entry is still
  used). The work dir then lives in memory (`/dev/shm`) and holds the download and the processed file only until
  the device write; an item whose estimate is above `file_max_bytes` is spooled to `/var/tmp` instead. The applier
  bounds how many prepared items wait (`files`, `budget_bytes`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from syncmusic.base.capabilities import OUTPUT_FIELDS, DeviceCapabilities
from syncmusic.base.context import AppContext
from syncmusic.base.library import Library
from syncmusic.base.processor import Processor
from syncmusic.core import books, registry
from syncmusic.core.models import (
    PlanItem,
    PlannedFile,
    ProcessorSlot,
    SyncSettings,
    Track,
    WorkItem,
)
from syncmusic.infra.cache import CacheEntry, ProcessingCache, make_key

log = logging.getLogger(__name__)

FORMAT_EXT = {"aac": "m4a", "alac": "m4a", "ogg": "ogg", "wma": "wma", "aiff": "aiff"}
LOSSLESS = frozenset({"flac", "alac", "wav", "aiff", "ape", "wv"})
MEDIA_NAME = "media"  # file name stem of the processed track inside a cache entry
ORIGINAL_NAME = "original"


def initial_plan_item(track: Track) -> PlanItem:
    """The plan item of an unprocessed track: the library file as it is."""
    ext = Path(track.path).suffix.lstrip(".").lower() if track.path else ""
    if not ext:
        ext = FORMAT_EXT.get(track.format, track.format or "bin")
    file = PlannedFile(
        format=track.format,
        codec=track.codec or track.format,
        ext=ext,
        size=track.size_bytes,
        bitrate_kbps=track.bitrate_kbps,
        sample_rate=track.sample_rate,
        bit_depth=track.bit_depth if track.format in LOSSLESS else None,
    )
    return PlanItem(track=track, file=file)


def base_item(item: PlanItem) -> PlanItem:
    """The unprocessed form of a (possibly planned) item: same track, rules and space-step flag."""
    return initial_plan_item(item.track).model_copy(
        update={"rules": list(item.rules), "enable_space_steps": item.enable_space_steps}
    )


def _json_default(o: object) -> object:
    if isinstance(o, set | frozenset):
        return sorted(o, key=str)  # set order differs between processes (hash randomisation)
    return str(o)


# the scheme `caps_hash` uses; manifest entries remember it (ManifestEntry.caps_v): an entry of an older scheme
# whose only changed part is "device" is kept and re-hashed, not replaced
CAPS_HASH_VERSION = 1


def caps_hash(caps: DeviceCapabilities) -> str:
    # only the fields that change the produced file (OUTPUT_FIELDS): paths, limits and new
    # informational fields are not, nor fields at their default value (a new field); the planner moves files for
    # a path layout change itself
    dump = caps.model_dump(mode="python", include=set(OUTPUT_FIELDS), exclude_defaults=True)
    data = json.dumps(dump, sort_keys=True, default=_json_default)
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def inputs_diff(old: dict[str, Any] | None, new: dict[str, Any] | None) -> str:
    """`a 1 → 2; codecs +opus -alac` for two `processing_inputs`; lists are shown as added and
    removed values. "" when nothing differs; unknown old values say so."""
    if new is None:
        return ""
    if old is None:
        return "previous values unknown (stored from now on)"
    out: list[str] = []
    for k in sorted(old.keys() | new.keys()):
        a, b = old.get(k), new.get(k)
        if a == b:
            continue
        if isinstance(a, list | type(None)) and isinstance(b, list | type(None)) and (a or b):
            plus = [f"+{v}" for v in (b or []) if v not in (a or [])]
            minus = [f"-{v}" for v in (a or []) if v not in (b or [])]
            out.append(f"{k} {' '.join(plus + minus)}")
        else:
            out.append(f"{k} {_short(a)} → {_short(b)}")
    return "; ".join(out)


def _short(v: object) -> str:
    if v is None:
        return "(none)"
    s = v if isinstance(v, str) else json.dumps(v, sort_keys=True, ensure_ascii=False)
    return s if len(s) <= 60 else s[:57] + "…"


class Chain:
    """A device's processor chain: enabled slots, instantiated in order."""

    def __init__(self, processors: Sequence[Processor]) -> None:
        self.processors = list(processors)

    @classmethod
    def from_slots(cls, slots: Sequence[ProcessorSlot], ctx: AppContext | None = None) -> Chain:
        return cls([registry.processor_class(s.type)(s.settings, ctx) for s in slots if s.enabled])

    @property
    def needs(self) -> frozenset[str]:
        out: set[str] = set()
        for p in self.processors:
            out |= p.needs
        return frozenset(out)

    def plan(self, item: PlanItem, caps: DeviceCapabilities) -> PlanItem:
        for p in self.processors:
            item = p.plan(item, caps)
        return item

    def _parts(self, item: PlanItem, caps: DeviceCapabilities) -> list[Any] | None:
        # Always fold from the unprocessed item: callers may pass an already planned item (the applier passes
        # `PlanAction.item`), on which a processor would add no step and drop out of the key.
        item = base_item(item)
        parts: list[Any] = [caps_hash(caps), item.enable_space_steps]
        for p in self.processors:
            planned = p.plan(item, caps)
            if len(planned.steps) == len(item.steps):
                continue  # adds no step for this item: its settings do not change the output
            item = planned
            key = p.cache_key(item)  # called with the item as planned after this processor
            if key is None:
                return None
            parts.append([p.type_name, p.version, key])
        return parts

    def processing(self, item: PlanItem, caps: DeviceCapabilities) -> dict[str, str]:
        """The parts of `chain_hash` by name: "device" (caps + space steps) and one per processor
        that plans a step. Comparing two of them names what changed; {} when the output is not cacheable."""
        parts = self._parts(item, caps)
        if parts is None:
            return {}
        out = {"device": make_key("device", parts[:2])[:16]}
        out.update((p[0], make_key("processor", p)[:16]) for p in parts[2:])
        return out

    def processing_inputs(
        self, item: PlanItem, caps: DeviceCapabilities
    ) -> dict[str, dict[str, Any]]:
        """What each `processing` part is made of, readable, keyed by the part's hash: the
        device's output caps and space steps, a processor's version, settings and planned output. The manifest
        keeps one copy per hash, so a changed part can say what changed (`inputs_diff`)."""
        base = base_item(item)
        hashes = self.processing(base, caps)
        if not hashes:
            return {}
        dump = caps.model_dump(mode="python", include=set(OUTPUT_FIELDS), exclude_defaults=True)
        device = json.loads(json.dumps(dump, sort_keys=True, default=_json_default))
        device["space_steps"] = base.enable_space_steps
        out = {hashes["device"]: device}
        planned = base
        for p in self.processors:
            nxt = p.plan(planned, caps)
            if len(nxt.steps) == len(planned.steps) or p.type_name not in hashes:
                continue
            planned = nxt
            inputs: dict[str, Any] = {"version": p.version, **p.settings.model_dump(mode="json")}
            key = p.cache_key(planned)
            if (
                key is not None and key != p.settings_hash()
            ):  # a readable planned output (transcode)
                inputs["output"] = key
            out[hashes[p.type_name]] = inputs
        return out

    def audio_stage(self, name: str) -> bool:
        """A `processing` part that changes the audio: the device's, or a processor of the audio stage."""
        p = next((p for p in self.processors if p.type_name == name), None)
        return p is None or p.stage == "audio"

    def chain_hash(self, item: PlanItem, caps: DeviceCapabilities) -> str:
        """Identity of the processing (not the source): changes when settings/versions/caps change.

        `item` may be the unprocessed or the planned item: both give the same hash (see `base_item`).
        Only processors that plan a step for the item count, so adding or changing a processor that does not
        apply to it (a FLAC-only transcode rule for an MP3) keeps the item on the device.
        """
        parts = self._parts(item, caps)
        return make_key("chain", parts if parts is not None else "uncacheable")[:16]

    def cache_key(self, item: PlanItem, caps: DeviceCapabilities) -> str | None:
        """Cache key of the processed output (None = not cacheable)."""
        parts = self._parts(item, caps)
        if parts is None:
            return None
        t = item.track
        return make_key("prepared", t.ref.library_id, t.ref.item_id, t.source_state, parts)


@dataclass(frozen=True)
class NoCache:
    """No-cache mode limits (device sync settings)."""

    files: int = 4  # prepared items held per sync
    budget_bytes: int = 1000 * 1000**2  # their estimated size together
    file_max_bytes: int = 256 * 1000**2  # above it an item is spooled to disk, not held in memory

    @classmethod
    def of(cls, s: SyncSettings) -> NoCache | None:
        if not s.no_cache:
            return None
        return cls(
            s.no_cache_files, s.no_cache_budget_mb * 1000**2, s.no_cache_file_max_mb * 1000**2
        )

    def in_memory(self, item: PlanItem) -> bool:
        return memory_estimate(item) <= self.file_max_bytes


def memory_estimate(item: PlanItem) -> int:
    """Bytes the work dir of `item` may hold: the original (when downloaded) plus the processed file."""
    return (item.track.size_bytes or 0) + (item.file.size or 0)


SPOOL_ROOT = Path("/var/tmp/syncmusic-spool")  # not /tmp: a small tmpfs on the Pi
MEM_ROOT = Path("/dev/shm/syncmusic-work")


class StepError(Exception):
    """A prepare step failed: `step` is download, cover, lyrics or the processor's type; the message is
    the cause's."""

    def __init__(self, step: str, cause: Exception) -> None:
        super().__init__(str(cause) or type(cause).__name__)
        self.step = step


async def _step[T](step: str, aw: Awaitable[T]) -> T:
    """Await `aw`; a failure (not a cancellation) becomes a StepError naming the step."""
    try:
        return await aw
    except StepError:
        raise
    except Exception as e:
        if "Cancelled" in type(e).__name__:
            raise
        raise StepError(step, e) from e


MAX_MERGES = 2
MERGE_SPEED = 60.0  # x realtime of the speech re-encode on a Pi 5


@dataclass
class Merge:
    """An audiobook merge in flight: the sync status shows it while the writer waits for it."""

    title: str
    total_s: float
    started: float = field(default_factory=lambda: time.monotonic())
    done_s: float = 0.0

    def fraction(self) -> float:
        """How much of the book is encoded, 0..1."""
        return min(1.0, self.done_s / self.total_s) if self.total_s > 0 else 0.0

    def status(self) -> str:
        """'Combining “Title” - 1.0 of 13.0 h, ~45 min left'."""
        hours = self.total_s >= 3600
        unit, div = ("h", 3600.0) if hours else ("min", 60.0)
        text = (
            f"Combining “{self.title}” - {self.done_s / div:.1f} of {self.total_s / div:.1f} {unit}"
        )
        took = time.monotonic() - self.started
        if self.done_s > 0 and took > 5:
            left = (self.total_s - self.done_s) * took / self.done_s
            text += f", ~{max(1, round(left / 60))} min left"
        return text


class Preparer:
    def __init__(
        self,
        cache: ProcessingCache,
        work_root: Path,
        mem_root: Path | None = None,
        spool_root: Path = SPOOL_ROOT,
    ) -> None:
        self.cache = cache
        self.work_root = work_root
        # no-cache mode: small items in memory, large ones spooled to disk
        self.mem_root = mem_root or (MEM_ROOT if MEM_ROOT.parent.is_dir() else work_root)
        self.spool_root = spool_root
        # Settings → 'Always cache combined audiobooks' (AppSettings.cache_combined_books)
        self.cache_books = True
        self._stale_size_warned: set[str] = set()  # libraries whose sizes are out of date
        # audiobook merges re-encode hours of audio: at most MAX_MERGES at once so the other prepares and the
        # device writer keep a core
        self._merges = asyncio.Semaphore(MAX_MERGES)
        self.merging: dict[str, Merge] = {}  # by book key (`lib/item`), while its ffmpeg runs

    @asynccontextmanager
    async def prepared(
        self,
        library: Library,
        item: PlanItem,
        chain: Chain,
        caps: DeviceCapabilities,
        check_cancelled: Callable[[], None] | None = None,
        no_cache: NoCache | None = None,
    ) -> AsyncIterator[WorkItem]:
        """Yield the item processed by `chain` (files valid until the context exits)."""
        root = self.work_root
        if no_cache is not None:
            root = self.mem_root if no_cache.in_memory(item) else self.spool_root
        work_dir = root / uuid.uuid4().hex
        key = chain.cache_key(item, caps)
        pinned: list[str] = []
        try:
            entry = await self.cache.get(key) if key else None
            if entry is not None:
                pinned.append(entry.key)
                async with self.cache.pinned(entry.key):
                    yield self._from_entry(entry, item, work_dir)
                return
            t0 = time.monotonic()
            work = await self._process(
                library, item, chain, caps, work_dir, pinned, check_cancelled, no_cache is None
            )
            t1 = time.monotonic()
            # no entry: no-cache mode, or no room next to the pinned entries; the work dir files go as they are
            store = key is not None and no_cache is None and work.steps_done
            if store and key is not None and (entry := await self._store(key, work)):
                work = self._from_entry(entry, item, work_dir)
                pinned.append(entry.key)
            # where a slow sync waits: download + processors, then the cache store
            log.debug(
                "prepared %s: processing %.2f s, cache store %.2f s",
                item.track.ref.key(),
                t1 - t0,
                time.monotonic() - t1,
            )
            async with self.cache.pinned(*pinned):
                yield work
        finally:
            await asyncio.to_thread(shutil.rmtree, work_dir, True)

    async def _process(
        self,
        library: Library,
        item: PlanItem,
        chain: Chain,
        caps: DeviceCapabilities,
        work_dir: Path,
        pinned: list[str],
        check_cancelled: Callable[[], None] | None,
        cache: bool = True,
    ) -> WorkItem:
        # made here, not in a thread: a cancel while a thread waits to make it would leave the directory behind
        # (made after the cleanup of the cancelled prepare)
        work_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
        t0 = time.monotonic()
        original = await _step("download", self.original(library, item.track, work_dir, cache))
        log.debug("original of %s: %.2f s", item.track.ref.key(), time.monotonic() - t0)
        if isinstance(original, CacheEntry):
            pinned.append(original.key)
            path = original.file(original.meta["name"])
        else:
            path = original
        # pinned from here on, before any await: another sync preparing the same track (two devices at once) puts the
        # same key, and a put replaces an unpinned entry - its file would vanish under this one's processors
        async with self.cache.pinned(*pinned):
            work = await self._inputs(
                library, WorkItem(plan=item, path=path, work_dir=work_dir), chain
            )
            for p in chain.processors:
                if check_cancelled is not None:
                    check_cancelled()
                work = await _step(p.type_name, p.run(work, caps))
        return work

    @asynccontextmanager
    async def retagged(
        self,
        library: Library,
        item: PlanItem,
        chain: Chain,
        caps: DeviceCapabilities,
        fetch: Callable[[Path], Awaitable[bool]],
        check_cancelled: Callable[[], None] | None = None,
    ) -> AsyncIterator[WorkItem | None]:
        """Yield `item` (planned) re-processed from the device's own file (`fetch` copies it to the given path) by
        the tag/cover/sidecar processors only; None when the device has no such file to start from."""
        work_dir = self.work_root / uuid.uuid4().hex
        try:
            work_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - not in a thread: see _process
            path = work_dir / f"{ORIGINAL_NAME}.{item.file.ext}"
            if not await _step("download", fetch(path)):
                yield None
                return
            audio = [p.type_name for p in chain.processors if p.stage == "audio"]
            work = WorkItem(plan=item, path=path, work_dir=work_dir, steps_done=audio)
            work = await self._inputs(library, work, chain)
            for p in chain.processors:
                if p.stage == "audio":
                    continue
                if check_cancelled is not None:
                    check_cancelled()
                work = await _step(p.type_name, p.run(work, caps))
            yield work
        finally:
            await asyncio.to_thread(shutil.rmtree, work_dir, True)

    async def _inputs(self, library: Library, work: WorkItem, chain: Chain) -> WorkItem:
        """Fetch the cover and lyrics the chain `needs` into the work item."""
        t, work_dir = work.track, work.work_dir
        if "cover" in chain.needs and t.has_cover:
            cover_of = next((p for p in books.parts_of(t) if p.has_cover), t)  # a book: its file's
            data = await _step("cover", library.open_cover(cover_of.ref.item_id))
            if data:
                cover = work_dir / "library-cover"
                await asyncio.to_thread(cover.write_bytes, data)
                work.cover = cover
        if "lyrics" in chain.needs and t.has_lyrics:
            work.lyrics = await _step("lyrics", library.get_lyrics(t.ref.item_id))
        return work

    async def original(
        self, library: Library, track: Track, work_dir: Path, cache: bool = True
    ) -> Path | CacheEntry:
        """The library's original file: a local path as is, or a remote download cached once (`cache`)."""
        key = make_key("original", track.ref.library_id, track.ref.item_id, track.source_state)
        entry = await self.cache.get(key)
        if entry is not None:
            return entry
        if books.is_book(track):
            # cached even in no-cache mode unless switched off: a merge re-encodes hours of audio
            return await self._merged_book(library, track, work_dir, key, cache or self.cache_books)
        src = await library.open_media(track.ref.item_id)
        if isinstance(src, Path):
            return src
        ext = initial_plan_item(track).file.ext
        name = f"{ORIGINAL_NAME}.{ext}"
        tmp = work_dir / name
        size = 0
        f = await asyncio.to_thread(tmp.open, "wb")
        try:
            async for chunk in src:
                await asyncio.to_thread(f.write, chunk)
                size += len(chunk)
        finally:
            await asyncio.to_thread(f.close)
        if track.size_bytes and size != track.size_bytes:
            # a complete download (the library checks its HTTP length) that differs from the library's size: the
            # file changed since the library's last scan; the file is what it serves now
            first = track.ref.library_id not in self._stale_size_warned
            self._stale_size_warned.add(track.ref.library_id)
            hint = "rescan the library"
            if first:
                try:
                    if await library.request_rescan():
                        hint = "a rescan of the library was started"
                except Exception as e:  # best effort: the warning matters, not the rescan
                    log.info("rescan of %r failed: %s", track.ref.library_id, e)
            log.log(
                logging.WARNING if first else logging.INFO,
                "download of %r from %r: got %d bytes, library says %d - the library's size is out of date "
                "(file changed since the last library scan?) - %s%s",
                track.ref.item_id,
                track.ref.library_id,
                size,
                track.size_bytes,
                hint,
                "; further mismatches of this library are logged at INFO" if first else "",
            )
        if not cache:
            return tmp
        # no room in the cache: the download stays in the work dir until the item is written
        return await self.cache.put(key, {name: tmp}, {"name": name}, move=True) or tmp

    async def _merged_book(
        self, library: Library, book: Track, work_dir: Path, key: str, cache: bool
    ) -> Path | CacheEntry:
        """The parts of a multi-file audiobook merged into one chaptered .m4b, cached like a download."""
        parts_dir = work_dir / "parts"
        parts_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - not in a thread: see _process
        files: list[Path] = []
        held: list[str] = []
        for part in books.parts_of(book):
            src = await self.original(library, part, parts_dir, cache=False)
            if isinstance(src, CacheEntry):  # downloaded before, as a single file
                held.append(src.key)
                src = src.file(src.meta["name"])
            elif src.parent == parts_dir:  # a download: unique name per part
                src = src.rename(parts_dir / f"{len(files)}{src.suffix}")
            files.append(src)
        name = f"{ORIGINAL_NAME}.m4b"
        out = work_dir / name
        async with self.cache.pinned(*held), self._merges:
            merge = Merge(book.title, book.duration_s or 0.0)
            self.merging[book.ref.key()] = merge
            try:
                await books.merge_book(book, files, out, lambda t: setattr(merge, "done_s", t))
            finally:
                self.merging.pop(book.ref.key(), None)
        await asyncio.to_thread(shutil.rmtree, parts_dir, True)
        if not cache:
            return out
        return await self.cache.put(key, {name: out}, {"name": name}, move=True) or out

    async def merge_warning(self, tracks: Sequence[Track]) -> str | None:
        """The plan's note on the audiobooks it will merge by re-encoding (not cached, not all AAC): how many and
        roughly how long the encoding takes, or None."""
        audio_s = 0.0
        n = 0
        for t in tracks:
            if not books.is_book(t) or all(p.codec == "aac" for p in books.parts_of(t)):
                continue
            key = make_key("original", t.ref.library_id, t.ref.item_id, t.source_state)
            if await self.cache.get(key) is None:
                n += 1
                audio_s += t.duration_s or 0.0
        if not n:
            return None
        minutes = max(1, round(audio_s / MERGE_SPEED / min(n, MAX_MERGES) / 60))
        books_ = "1 audiobook" if n == 1 else f"{n} audiobooks"
        return (
            f"combining {books_} ({audio_s / 3600:.1f} h of audio) into .m4b: about {minutes} min of encoding "
            "before they are written"
        )

    async def _store(self, key: str, work: WorkItem) -> CacheEntry | None:
        files: dict[str, Path] = {f"{MEDIA_NAME}.{work.plan.file.ext}": work.path}
        sidecars: dict[str, str] = {}
        for i, (sk, path) in enumerate(sorted(work.sidecars.items())):
            name = f"sidecar{i}{path.suffix}"
            files[name] = path
            sidecars[sk] = name
        cover = None
        if work.cover is not None:
            cover = f"cover{work.cover.suffix}"
            files[cover] = work.cover
        meta = {
            "media": f"{MEDIA_NAME}.{work.plan.file.ext}",
            "file": work.plan.file.model_dump(mode="json"),
            "sidecars": sidecars,
            "cover": cover,
            "steps_done": work.steps_done,
            "notes": work.notes,
        }
        # copy, not move: the media file may still be a library original or another cache entry
        return await self.cache.put(key, files, meta)

    @staticmethod
    def _from_entry(entry: CacheEntry, item: PlanItem, work_dir: Path) -> WorkItem:
        m = entry.meta
        return WorkItem(
            plan=item.model_copy(update={"file": PlannedFile.model_validate(m["file"])}),
            path=entry.file(m["media"]),
            work_dir=work_dir,
            cover=entry.file(m["cover"]) if m.get("cover") else None,
            sidecars={k: entry.file(v) for k, v in m.get("sidecars", {}).items()},
            steps_done=list(m.get("steps_done", [])),
            notes=dict(m.get("notes", {})),
        )

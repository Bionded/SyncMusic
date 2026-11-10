# SPDX-License-Identifier: GPL-3.0-or-later
"""Sync service: registered devices, connected disks, plan → confirm → apply as a job.

- Devices: registered ones (store) + connected unknown disks (detector). `register` turns a probe result (or a
  fixed directory, `mount_path`) into a `DeviceConfig` with suggested processors.
- Opening a device mounts its data partition under `<mount_root>/<device id>` (or reuses an existing mount, or
  uses `DeviceConfig.mount_path`); mounts stay until eject / disconnect.
- `plan` checks every attached library (a failing one aborts: no partial sync), loads snapshots, reads the
  manifest (a newer manifest version raises `ManifestTooNew` with the user's two choices) and computes the
  plan with space (`core/space.py`). Plans live in memory until applied.
- `apply` runs as a job (one per device): first sync options `keep` (unknown files stay), `adopt` (files that
  match a library item by artist+album+title+duration ±2 s become managed) or `wipe` (device emptied first).
  The SyncReport is stored.
- on connect: `auto` → plan + apply unless the plan needs confirmation or does not fit (then
  `sync.needs_confirmation`), `ask` → `sync.needs_confirmation`, `manual` → nothing.
Reverse sync is added in M7 before the plan.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import errno
import functools
import hashlib
import logging
import math
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.base.context import AppContext
from syncmusic.base.device import UNKNOWN_SPACE, Device, ProbeResult
from syncmusic.base.feedback import FeedbackSink
from syncmusic.base.library import Library
from syncmusic.base.progress import on_progress
from syncmusic.core import contents as player_contents
from syncmusic.core import device_templates, plan_view, planprogress, registry, update_status
from syncmusic.core import health as storage_health
from syncmusic.core import lease as device_lease
from syncmusic.core import manifest as manifest_io
from syncmusic.core import positions as book_positions
from syncmusic.core.activity import report_activity
from syncmusic.core.applier import (
    Applier,
    DeviceLostError,
    describe,
    gentle_pacer,
    storage_failure,
    track_ref,
)
from syncmusic.core.detection import (
    ConnectedDisk,
    Detector,
    MountedProbe,
    manifest_id_at,
    probe_disk,
)
from syncmusic.core.fit import TARGETS as FIT_TARGETS
from syncmusic.core.fit import FitTarget, with_fit_rule
from syncmusic.core.models import (
    COMBINE_AUDIOBOOKS_TYPES,
    ActionKind,
    DeviceConfig,
    DeviceContents,
    DeviceIdentity,
    DeviceItem,
    ErasedMark,
    HealthStatus,
    InstanceInfo,
    ItemFailure,
    ItemRef,
    LibraryAccount,
    LibraryConfig,
    LibrarySnapshot,
    Manifest,
    ManifestEntry,
    ManifestHeader,
    MediaKind,
    PlanAction,
    PlayEvent,
    PlaylistConflict,
    PositionInfo,
    ProcessorSlot,
    ReverseReport,
    RuleSet,
    RuleTemplate,
    SyncPlan,
    SyncReport,
    SyncSettings,
    Track,
    utcnow,
)
from syncmusic.core.mount_path import (
    device_tree_reason,
    on_removable_disk,
    overlaps,
    protected_reason,
)
from syncmusic.core.plan_adjust import (
    defer_device_rewrites,
    ratings_after_reverse,
    removed_played,
    update_only,
    update_signature,
    update_writes_items,
)
from syncmusic.core.planner import meta_hash, meta_snapshot, over_delete_threshold, reverse_changes
from syncmusic.core.playlist_reverse import Policy, playlists_after_reverse, push_playlists
from syncmusic.core.prepare import CAPS_HASH_VERSION, Chain, NoCache, Preparer, initial_plan_item
from syncmusic.core.proc_templates import DEFAULT_PROCESSOR_TEMPLATES, with_tag_step
from syncmusic.core.reverse import ReverseSync
from syncmusic.core.rules import defaults as rule_defaults
from syncmusic.core.rules import evaluate_ruleset, validate_ruleset
from syncmusic.core.rules.evaluator import resolve_rules
from syncmusic.core.rules.templates import (
    add_device_exclusions,
    as_rules,
    device_libraries,
    own_template,
    own_template_id,
)
from syncmusic.core.snapshots import SnapshotCache
from syncmusic.core.space import (
    check_db_size,
    db_level,
    items_after,
    left_off,
    plan_with_space,
    safety_margin,
    usage_breakdown,
)
from syncmusic.core.windows import in_window, next_start
from syncmusic.infra.blockdev import format as fmt
from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev import scsi
from syncmusic.infra.blockdev.guard import Guard, GuardError
from syncmusic.infra.blockdev.identify import BlockDeviceInfo, find_disk_of, scan_block_devices
from syncmusic.infra.blockdev.mtp import is_mtp
from syncmusic.infra.blockdev.scsi import TimeSyncRejected
from syncmusic.infra.jobs import JobCancelled, JobContext, JobInfo, JobStatus
from syncmusic.infra.store import Store
from syncmusic.infra.watch import watch_folder

# how often the due library snapshots are looked for (each is refreshed every 30 min)
SNAPSHOT_LOOP_S = 60
CLOCK_FRESH_S = 30 * 60  # a device clock set this recently is not set again at a sync or eject
log = logging.getLogger(__name__)

NO_MEDIUM_HINT = "connected, but its storage is not shared: turn on USB storage on the player"

FirstSync = Literal["keep", "adopt", "wipe"]
MATCH_DURATION_S = 2.0
# longest device name a Rename accepts; the file system label is shortened further per file system
NAME_MAX = 64
# device types the Wipe action (instead of Format) is for -> what it keeps
# Android (MTP): only files in our base folder go; the rest of the phone is never touched
WIPE_KEEPS = {
    "rockbox": "/.rockbox",
    "hiby": "/.local",
    "android": "everything outside its SyncMusic folder",
}


def _free_id(name: str, taken: Mapping[str, Any]) -> str:
    """A template id from `name` that is not taken (and not a library default's)."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "template"
    new_id, n = base, 2
    while new_id in taken or new_id.startswith(rule_defaults.PREFIX):
        new_id, n = f"{base}-{n}", n + 1
    return new_id


class SyncError(RuntimeError):
    """A sync precondition failed (unknown device, library down, busy, needs confirmation ...)."""

    def __init__(self, message: str, code: str = "sync_error") -> None:
        super().__init__(message)
        self.code = code


class RegisterRequest(BaseModel):
    # an unknown field (a typo, or a DeviceConfig field set later with PATCH) is refused, never dropped
    model_config = ConfigDict(extra="forbid")

    name: str
    disk: str | None = None  # /dev path of a connected disk (from GET /devices)
    mount_path: str | None = None  # or: a directory used as the device
    type: str | None = None  # default: the best probe result
    library_ids: list[str] = []  # link each library's first default template (its "All …")
    rule_set: RuleSet = RuleSet()  # templates only: links, mode, priority, limits
    processors: list[ProcessorSlot] | None = None  # None = suggested for the device's capabilities
    # link a processor template: its chain is copied into the device and follows the template
    processor_template_id: str | None = None
    on_connect: Literal["auto", "ask", "manual"] | None = None  # None = settings.default_on_connect
    settings: dict[str, Any] = {}
    # the first sync's choice made on Register: the plan review preselects it
    first_sync_choice: Literal["keep", "adopt", "wipe"] | None = None
    # a device synced before (its manifest has a device uid): continue with its manifest (default), start fresh
    # (wipe, home instance only) or keep its files as unknown ones under a new identity
    continuity: Literal["continue", "fresh", "keep"] | None = None


@dataclass
class PlanContext:
    plan: SyncPlan
    matches: list[tuple[DeviceItem, Track]]  # first sync: existing files that match library items
    # first sync: media files already on the device (none: nothing to keep / adopt / wipe)
    existing: int = 0
    # the device was synced by another registration (needs a confirmation, an update sync skips it)
    foreign: bool = False
    # manifest keys the plan knew to be missing on the device and does not write: a warning, not a failed read-back
    waiting: set[str] = field(default_factory=set)
    # the review's choice per playlist conflict, given with apply
    playlist_choices: dict[str, Policy] = field(default_factory=dict)


def _only_keeps(pc: PlanContext) -> bool:
    """A plan with nothing to review: every action a plain keep, no confirmation, no playlist choice."""
    p = pc.plan
    return not (
        p.first_sync
        or p.needs_confirmation
        or p.playlist_conflicts
        or any(x.change != "same" for x in p.playlist_changes)
        or pc.playlist_choices
        or pc.waiting
        or any(a.kind is not ActionKind.KEEP or a.restamp or a.rehash for a in p.actions)
    )


def _slug(name: str) -> str:
    """Device id from its name: words in any script joined by '-', like template ids."""
    s = re.sub(r"[\W_]+", "-", name.casefold()).strip("-")
    return s[:24].strip("-") or "device"


def _fold(v: str | None) -> str:
    return (v or "").casefold().strip()


def foreign_manifest(manifest: Manifest, cfg: DeviceConfig) -> bool:
    """The manifest was written by another registration: its uid is not this config's. Every
    registration writes its uid, so a manifest without one is foreign too (no pre-uid installs)."""
    return manifest.header.device_uid != cfg.identity.manifest_device_id


is_home = manifest_io.is_home


def home_name(manifest: Manifest | None) -> str:
    home = manifest.header.home if manifest is not None else None
    return (home.name or home.id) if home is not None else ""


def match_existing(contents: DeviceContents, tracks: list[Track]) -> list[tuple[DeviceItem, Track]]:
    """Files already on the device that are library items: same artist + album + title, duration ±2 s."""
    index: dict[tuple[str, str, str], list[Track]] = {}
    for t in tracks:
        index.setdefault((_fold(t.artist), _fold(t.album), _fold(t.title)), []).append(t)
    out: list[tuple[DeviceItem, Track]] = []
    used: set[str] = set()
    for item in contents.items:
        for t in index.get((_fold(item.artist), _fold(item.album), _fold(item.title)), []):
            if t.ref.key() in used:
                continue
            d1, d2 = item.duration_s, t.duration_s
            if d1 is not None and d2 is not None and abs(d1 - d2) > MATCH_DURATION_S:
                continue
            out.append((item, t))
            used.add(t.ref.key())
            break
    return out


def suggested_processors(caps: DeviceCapabilities) -> list[ProcessorSlot]:
    out: list[ProcessorSlot] = []
    for cls in registry.processor_classes():
        slot = cls.suggest(caps)
        if slot is not None:
            out.append(slot)
    order = {"audio": 0, "tags": 1, "cover": 2, "sidecar": 3}
    stage = {c.type_name: order[c.stage] for c in registry.processor_classes()}
    return with_tag_step(sorted(out, key=lambda s: stage.get(s.type, 9)))


def disk_key(disk: BlockDeviceInfo) -> str:
    """How an ignored disk is remembered: its USB serial, else its file system UUID, else its path (a
    disk without either is forgotten when it gets another path)."""
    return (
        f"serial:{disk.serial}"
        if disk.serial
        else f"uuid:{disk.uuid}"
        if disk.uuid
        else f"path:{disk.path}"
    )


# a read or write the device's storage refused: a damaged file system (FAT "invalid cluster chain") or failing
# storage; EROFS: the kernel remounted it read-only after such errors
FS_DAMAGE_ERRNOS = (errno.EIO, errno.EUCLEAN, errno.EROFS)


def fs_damage(e: BaseException) -> OSError | None:
    """The OSError in `e`'s cause chain that says the device's file system is damaged, if any."""
    seen: set[int] = set()
    cur: BaseException | None = e
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, OSError) and cur.errno in FS_DAMAGE_ERRNOS:
            return cur
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return None


CONNECT_CHECK_TIMEOUT_S = 120.0


def probe_fs(mount: Path, files: Iterable[str]) -> None:
    """OSError (EROFS) when `mount` is mounted read-only, or the read error of one of `files` (missing ones are
    skipped)."""
    if os.statvfs(mount).f_flag & os.ST_RDONLY:
        raise OSError(errno.EROFS, os.strerror(errno.EROFS), str(mount))
    for rel in files:
        try:
            f = (mount / rel).open("rb")
        except FileNotFoundError:
            continue
        with f:
            while f.read(1 << 20):
                pass


def suggests_gentle(cfg: DeviceConfig, e: BaseException) -> bool:
    """A failed sync should offer gentle write mode: its storage failed or it dropped off, the
    mode is off and the device is a disk (not FTP)."""
    return (
        not cfg.sync.gentle_write
        and not (cfg.type in registry.DEVICES and registry.device_class(cfg.type).network)
        and storage_failure(e)
    )


# sync settings that only change how a plan is applied, read from the device's config when it is:
# changing them keeps a pending plan
APPLY_ONLY_SYNC_FIELDS = frozenset(
    {
        "gentle_write",
        "gentle_batch_files",
        "gentle_pause_s",
        "gentle_rate_mb_s",
        "verify",
        "no_cache",
        "no_cache_files",
        "no_cache_budget_mb",
        "no_cache_file_max_mb",
        "eject_after",
        "backup_device_db",
        "backup_keep",
    }
)


def _plan_shape(cfg: DeviceConfig) -> dict[str, Any]:
    """The device config without the apply-only settings: when it changes, pending plans are outdated."""
    return cfg.model_dump(mode="json", exclude={"sync": set(APPLY_ONLY_SYNC_FIELDS)})


def _settings_identity(cls: type[Device], settings: dict[str, Any]) -> DeviceIdentity:
    """An MTP player added by hand: its USB serial setting is its identity, so it is recognised when
    it is plugged in. Other devices without a probe: none."""
    serial = settings.get("usb_serial")
    if cls.usb_mtp and isinstance(serial, str) and serial.strip():
        return DeviceIdentity(usb_serial=serial.strip())
    return DeviceIdentity()


def account_key(library_id: str, device: DeviceConfig | None) -> str:
    """The library's instance / snapshot cache key for this device: `<id>@<account id>` when the device signs in
    as one of the library's accounts (devices of the same user share it), else the library
    id."""
    account = device.library_accounts.get(library_id) if device is not None else None
    return f"{library_id}@{account}" if account else library_id


def _account_name(settings: dict[str, Any], fallback: str) -> str:
    """A migrated device account is named after its user."""
    for key in ("username", "user", "name"):
        if isinstance(settings.get(key), str) and settings[key]:
            return str(settings[key])
    return fallback


class SyncService:
    def __init__(
        self, ctx: AppContext, store: Store, preparer: Preparer, snapshots: SnapshotCache
    ) -> None:
        self.ctx = ctx
        self.store = store
        self.preparer = preparer
        self.snapshots = snapshots
        self._snapshot_loop: asyncio.Task[None] | None = None
        # libraries whose source id was read since the start (once per run, at their first plan)
        self._source_checked: set[str] = set()
        # local folder libraries watched for changes: library id -> (root, watch task)
        self._watchers: dict[str, tuple[Path, asyncio.Task[None]]] = {}
        # device clock: when it was last set, and the set-before-the-first-mount running after a connect
        self._clock_set: dict[str, float] = {}
        self._clock_tasks: dict[str, asyncio.Task[bool]] = {}
        self._foreign_tasks: dict[str, asyncio.Task[None]] = {}
        # Player contents being read, per device: a request that arrives meanwhile (the page was
        # reloaded) waits for the same read instead of starting another one
        self._contents_reads: dict[str, asyncio.Task[list[dict[str, Any]]]] = {}
        # Player contents per device with the stamp it was read at: tab switches and covers
        # answer from it until the device's manifest or database changes
        self._contents_cache: dict[str, tuple[str, list[dict[str, Any]], dict[str, str]]] = {}
        # per device: (manifest size, mtime) -> key -> path, so a content_file miss parses it once
        self._manifest_paths: dict[str, tuple[tuple[int, int], dict[str, str]]] = {}
        self.planning = planprogress.Planning(ctx.bus)  # plans started from the UI, with progress
        self._device_items: dict[str, frozenset[str]] = {}  # last recorded per device
        self.detector = Detector(ctx.bus, lambda: self._configs, self._probe_manifest)
        self._configs: list[DeviceConfig] = []
        self._libraries: dict[str, Library] = {}
        self._mounts: dict[str, mnt.Mounted] = {}  # device id -> our mount
        # device id -> lock around "check the cache, mount": concurrent opens share one mount
        self._mount_locks: dict[str, asyncio.Lock] = {}
        self._errors: dict[str, str] = {}  # device id -> why the connected device is unusable
        # device id -> the I/O error that showed its file system is damaged: no automatic syncs until a
        # re-plug, a repair, a format or a wipe clears it
        self._damaged: dict[str, str] = {}
        # network devices whose server did not answer the last scheduled start, and since when
        self._offline: dict[str, dt.datetime] = {}
        self._ignored: set[str] = set()  # unknown disks the user ignores, see disk_key
        self._ejecting: set[str] = set()  # never mounted again until the eject is over
        # ejected (powered off) but its disk not yet reported gone: never mounted again until unplugged
        self._ejected: set[str] = set()
        # one instance per plugged MTP player: it holds the player's only MTP session, so every job
        # shares it; closed on eject, unplug or a settings change
        self._mtp_open: dict[str, Device] = {}
        # device id -> {reason, plan_id} of a connect-time sync that waits for the user
        self._waiting: dict[str, dict[str, Any]] = {}
        self._scheduled_on: dict[
            str, dt.date
        ] = {}  # device id -> the day its daily sync last started
        self.plans: dict[str, PlanContext] = {}
        self.reverse_sync = ReverseSync(store)
        self.reverse_sync.on_delivered = self._patch_snapshot
        # update syncs: what the last one wrote, when it was checked, devices whose libraries changed
        self._update_sig: dict[str, str] = {}
        self._update_last: dict[str, float] = {}
        self._update_ran: dict[
            str, float
        ] = {}  # when the last update was started (not just first seen)
        self._update_dirty: set[str] = set()
        # device id -> why: an automatic full sync that came outside its sync windows and starts in the next one
        self._window_pending: dict[str, str] = {}
        # an update waits for the running jobs: no scheduled, periodic or on-connect sync starts meanwhile (the
        # on-connect ones wait like outside a sync window and start once it is lifted)
        self.hold_automatic = False
        self._update_tasks: set[asyncio.Task[Any]] = set()
        self._update_spawned: dict[
            str, asyncio.Task[Any]
        ] = {}  # device id -> its latest update trigger

    async def add_tag_steps(self) -> None:
        """Startup: stored processor templates and device chains from before get the tag step."""
        for tpl in (await self.store.processor_templates()).values():
            chain = with_tag_step(tpl.processors)
            if not tpl.builtin and chain is not tpl.processors:
                log.info("processor template %s: tag step added", tpl.id)
                await self.store.put_processor_template(
                    tpl.model_copy(update={"processors": chain})
                )
        for cfg in await self.store.devices():
            chain = with_tag_step(cfg.processors)
            if chain is not cfg.processors:
                log.info("%s: tag step added to its processors", cfg.id)
                await self.store.put_device(cfg.model_copy(update={"processors": chain}))

    async def load(self) -> None:
        await self._migrate_accounts()
        self._configs = await self.store.devices()
        self._ignored = set(await self.store.ignored_disks())
        # browsing caches: a reloaded snapshot updates open pages; loaded libraries refresh every 30 min
        self.snapshots.on_updated = self._library_updated
        if self._snapshot_loop is None:  # load() runs again after every device change
            self._snapshot_loop = asyncio.create_task(
                self._refresh_snapshots(), name="snapshot-refresh"
            )
        await self.sync_watchers()

    async def sync_watchers(self) -> None:
        """Watch every enabled local folder library with `watch` on; stop the others. A change
        reloads its snapshot in the background ~30 s later, like the periodic refresh would (devices using it get an update check)."""
        wanted: dict[str, Path] = {}
        for lib in await self.store.libraries():
            s = lib.settings or {}
            root = s.get("root")
            local = lib.type == "folder" and s.get("source", "local") == "local"
            if (
                local
                and lib.enabled
                and root
                and s.get("watch", True)
                and await asyncio.to_thread(Path(root).is_dir)
            ):
                wanted[lib.id] = Path(root)
        for lid, (root, task) in list(self._watchers.items()):
            if wanted.get(lid) != root or task.done():
                task.cancel()
                del self._watchers[lid]
        for lid, root in wanted.items():
            if lid not in self._watchers:
                task = asyncio.create_task(
                    watch_folder(root, functools.partial(self._folder_changed, lid)),
                    name=f"watch-{lid}",
                )
                self._watchers[lid] = (root, task)

    async def _folder_changed(self, library_id: str) -> None:
        log.info("library %s: files changed, reloading its snapshot", library_id)
        with contextlib.suppress(SyncError):  # removed meanwhile
            self.snapshots.refresh_soon(await self.library(library_id))

    def _library_updated(self, library_id: str) -> None:
        self.ctx.bus.publish("library.updated", library_id=library_id)
        task = asyncio.get_running_loop().create_task(self.mark_libraries_changed([library_id]))
        self._update_tasks.add(task)  # devices using it get an update check
        task.add_done_callback(self._update_tasks.discard)

    async def _refresh_snapshots(self) -> None:
        stop = (
            asyncio.Event()
        )  # never set: a timed wait that a patched asyncio.sleep (tests) cannot turn into a spin
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), SNAPSHOT_LOOP_S)
            for library_id in self.snapshots.due():
                try:
                    self.snapshots.refresh_soon(await self.library(library_id))
                except SyncError:  # removed meanwhile
                    continue

    async def _probe_manifest(self, disk: BlockDeviceInfo) -> MountedProbe | None:
        """Manifest id and device probes of an unmounted disk: mount each candidate partition read-only,
        read, unmount. Only with device detection on, only
        through the guard, and only if a fresh scan still shows the same disk (serial) and partition (fs UUID)
        at that path."""
        if self.ctx.guard is None or not self.ctx.config.detect_devices:
            return None
        current = {d.name: d for d in await asyncio.to_thread(scan_block_devices, False)}
        now = current.get(disk.name)
        if now is None or now.serial != disk.serial:
            return None
        nodes_now = {n.path: n for n in (now, *now.children)}
        result, fresh_disk = MountedProbe(), now

        def read(root: Path) -> MountedProbe:
            return MountedProbe(manifest_id_at(root), probe_disk(fresh_disk, root))

        for node in (*disk.partitions(), disk):
            if node.fstype not in mnt.MOUNT_TYPES or node.mountpoints:
                continue
            fresh = nodes_now.get(node.path)
            if fresh is None or fresh.uuid != node.uuid:
                continue
            try:
                found = await mnt.probe_read(
                    self.ctx.guard, node.path, self.ctx.config.mount_root, read
                )
            except (GuardError, mnt.BlockdevError) as e:
                log.info("not probing %s: %s", node.path, e)
                continue
            result.manifest_id = result.manifest_id or found.manifest_id
            result.probes = result.probes or found.probes
            if result.manifest_id and result.probes:
                break
        return result if result.manifest_id or result.probes else None

    async def close(self) -> None:
        """Unmount what we mounted and close library connections."""
        if self._snapshot_loop is not None:
            self._snapshot_loop.cancel()
            self._snapshot_loop = None
        for _, task in self._watchers.values():
            task.cancel()
        self._watchers.clear()
        for device_id in list(self._mounts):
            await self._unmount(device_id)
        for device_id in list(self._mtp_open):
            await self._close_mtp(device_id)
        for lib in self._libraries.values():
            try:
                await lib.close()
            except Exception:  # noqa: BLE001 - shutting down
                log.debug("closing library %s failed", lib.id, exc_info=True)
        self._libraries.clear()

    # --- libraries -------------------------------------------------------------------------------

    async def library(self, library_id: str, device: DeviceConfig | None = None) -> Library:
        """The library's instance; with a device that has its own account there, an instance
        signed in as that account (settings overridden), cached and snapshotted apart (`Library.cache_key`)."""
        account_id = device.library_accounts.get(library_id) if device is not None else None
        return await self.library_as(library_id, account_id)

    async def library_as(self, library_id: str, account_id: str | None = None) -> Library:
        """The library's instance signed in as one of its accounts (None = the main account)."""
        key = f"{library_id}@{account_id}" if account_id else library_id
        lib = self._libraries.get(key)
        if lib is not None:
            return lib
        cfg = await self.store.library(library_id)
        if cfg is None:
            raise SyncError(f"unknown library {library_id!r}", "not_found")
        if account_id:
            account = cfg.accounts.get(account_id)
            if account is None:
                raise SyncError(f"library {cfg.name!r} has no account {account_id!r}", "not_found")
            cfg = cfg.model_copy(update={"settings": {**cfg.settings, **account.settings}})
        lib = registry.library_class(cfg.type)(cfg, self.ctx)
        lib.account = account_id
        self._libraries[key] = lib
        return lib

    async def refresh_source_id(self, lib: Library, timeout_s: float = 15.0) -> str | None:
        """Read the source id of a library's main account and store it: the first one read becomes
        `source_id`; a different one later is kept as `source_id_now` and logged, never silently swapped.
        Best effort: an unreachable library leaves the config as it is."""
        if lib.account:
            return lib.config.source_id
        try:
            sid = await asyncio.wait_for(lib.source_id(), timeout_s)
        except Exception as e:  # noqa: BLE001 - optional information
            log.debug("library %s: source id not read: %s", lib.id, lib.failure_reason(e))
            return lib.config.source_id
        self._source_checked.add(lib.id)
        cfg = await self.store.library(lib.id)
        if cfg is None or sid is None:
            return sid
        if cfg.source_id is None:
            update: dict[str, str | None] = {"source_id": sid, "source_id_now": None}
        elif sid != cfg.source_id:
            if sid != cfg.source_id_now:
                log.warning(
                    "library %s now points to another source (%s, was %s)",
                    lib.id,
                    sid,
                    cfg.source_id,
                )
            update = {"source_id_now": sid}
        else:
            update = {"source_id_now": None}
        if all(getattr(cfg, k) == v for k, v in update.items()):
            return sid
        cfg = cfg.model_copy(update=update)
        await self.store.put_library(cfg)
        for cached in self._libraries.values():
            if cached.id == lib.id:
                cached.config = cached.config.model_copy(update=update)
        return sid

    async def _migrate_accounts(self) -> None:
        """Credentials stored per device become accounts of the library: one per distinct
        settings, named after the user name, and the device picks it; nothing is lost."""
        for dev in await self.store.devices():
            if not dev.legacy_library_accounts:
                continue
            picks = dict(dev.library_accounts)
            for lid, settings in dev.legacy_library_accounts.items():
                lib = await self.store.library(lid)
                if lib is None or not settings:
                    continue
                same = [a for a, acc in lib.accounts.items() if acc.settings == settings]
                if same:
                    picks[lid] = same[0]
                    continue
                aid = dev.id
                while aid in lib.accounts:
                    aid += "_"
                name = _account_name(settings, dev.name)
                accounts = {**lib.accounts, aid: LibraryAccount(name=name, settings=settings)}
                await self.store.put_library(lib.model_copy(update={"accounts": accounts}))
                picks[lid] = aid
                log.info(
                    "device %s: its own %s account moved into the library as %r", dev.id, lid, name
                )
            await self.store.put_device(
                dev.model_copy(update={"library_accounts": picks, "legacy_library_accounts": {}})
            )

    async def check_unsaved_library(self, cfg: LibraryConfig) -> HealthStatus:
        """Health of a library configuration that is not stored (Test connection with edited settings):
        a temporary instance, closed again; nothing is cached or stored."""
        lib = registry.library_class(cfg.type)(cfg, self.ctx)
        try:
            return await lib.check()
        finally:
            await lib.close()

    def memory_caches(self) -> list[dict[str, Any]]:
        """The biggest in-memory caches, with how many entries each holds (Storage & resources)."""
        from syncmusic.devices import _filedevice

        snaps = self.snapshots.memory_counts()
        manifests = manifest_io.cached_counts()
        return [
            {
                "id": "snapshots",
                "label": "Library snapshots",
                "entries": snaps[0],
                "items": snaps[1],
            },
            {
                "id": "contents",
                "label": "Player contents",
                "entries": len(self._contents_cache),
                "items": sum(len(c[1]) for c in self._contents_cache.values()),
            },
            {
                "id": "tags",
                "label": "File tags",
                "entries": len(_filedevice._TAG_CACHE),
                "items": len(_filedevice._TAG_CACHE),
            },
            {
                "id": "manifests",
                "label": "Device manifests",
                "entries": manifests[0],
                "items": manifests[1],
            },
            {"id": "plans", "label": "Plans", "entries": len(self.plans), "items": len(self.plans)},
        ]

    def drop_memory_caches(self) -> None:
        """Drop what is rebuilt on demand: snapshots in memory (reloaded from the database), Player contents,
        file tags, cached device manifests. A pending plan that only keeps what is on the device is dropped too
        (it holds every track and manifest entry, and planning it again is quick); other plans stay (a pending plan
        with changes is the user's work)."""
        from syncmusic.devices import _filedevice

        for pid in [p for p, c in self.plans.items() if _only_keeps(c)]:
            del self.plans[pid]

        self.snapshots.drop_memory()
        self._contents_cache.clear()
        _filedevice._TAG_CACHE.clear()
        manifest_io.drop_cache()

    def forget_library(self, library_id: str) -> None:
        self._source_checked.discard(library_id)
        for key in [
            k for k in self._libraries if k == library_id or k.startswith(library_id + "@")
        ]:
            self._libraries.pop(key)

    async def library_changed(self, library_id: str) -> None:
        """A library was edited or deleted: its instance is closed, its snapshot and the plans of its devices
        dropped."""
        self._source_checked.discard(library_id)
        for key in [
            k for k in self._libraries if k == library_id or k.startswith(library_id + "@")
        ]:
            await self._libraries.pop(key).close()  # devices' own accounts too
        await self.snapshots.invalidate(library_id)
        await self.sync_watchers()  # a new root, watch switched off, deleted
        known = [c.id for c in await self.store.libraries()] + [library_id]  # also a deleted one
        for d in self._configs:
            if library_id in await self.libraries_of(d, known):
                self.drop_plans(d.id)

    # --- devices ---------------------------------------------------------------------------------

    async def config(self, device_id: str) -> DeviceConfig:
        cfg = await self.store.device(device_id)
        if cfg is None:
            raise SyncError(f"unknown device {device_id!r}", "not_found")
        return cfg

    def _network(self, cfg: DeviceConfig) -> bool:
        """Reached over the network (FTP): no disk, no folder, nothing to mount or eject."""
        return cfg.type in registry.DEVICES and registry.device_class(cfg.type).network

    def _usb_mtp(self, cfg: DeviceConfig) -> bool:
        """An MTP player on USB: no disk to mount, but connected only while udev sees it."""
        return cfg.type in registry.DEVICES and registry.device_class(cfg.type).usb_mtp

    def _network_dir(self, cfg: DeviceConfig) -> Path:
        """The local placeholder `mount` of a network device (it keeps nothing there)."""
        return self.ctx.config.data_dir / "network" / cfg.id

    async def check_network(self, cfg: DeviceConfig) -> None:
        """Log in to a server device with `cfg`'s settings (registration, Device settings' Test and save)."""
        device = registry.device_class(cfg.type)(cfg, self._network_dir(cfg), self.ctx)
        try:
            await device.check()
        except Exception as e:  # noqa: BLE001 - the server's reason goes to the user
            raise SyncError(f"{cfg.name}: cannot reach the server: {e}", "not_connected") from e
        finally:
            await device.close()

    async def _reachable(self, cfg: DeviceConfig) -> bool:
        """Before an automatic start: a network device's server answers a login; unreachable is
        remembered as "offline since" and skipped quietly (the next slot tries again). Other devices: True."""
        if not self._network(cfg):
            return True
        try:
            await asyncio.wait_for(self.check_network(cfg), NETWORK_PROBE_S)
        except (SyncError, TimeoutError) as e:
            if cfg.id not in self._offline:
                log.info("%s is not reachable, automatic syncs skip it: %s", cfg.id, e)
                self._offline[cfg.id] = dt.datetime.now(dt.UTC)
            return False
        if self._offline.pop(cfg.id, None) is not None:
            log.info("%s is reachable again", cfg.id)
        return True

    async def _library_defaults(self, library_ids: list[str]) -> list[str]:
        """Each library's first default template (its "All …")."""
        templates = await self.store.templates()
        out = []
        for lid in library_ids:
            if await self.store.library(lid) is None:
                raise SyncError(f"unknown library {lid!r}", "invalid")
            tid = next((t.id for t in templates.values() if t.default_of == lid), None)
            if tid is None:
                raise SyncError(f"library {lid!r} has no default template", "invalid")
            out.append(tid)
        return out

    async def set_own_rules(self, cfg: DeviceConfig, rules: RuleSet) -> DeviceConfig:
        """Store the device's picks and removals in its own template and link it to the device;
        returns the device."""
        tid = own_template_id(cfg.id)
        await self.store.put_template(own_template(cfg, await self.store.template(tid), rules))
        self.drop_plans(cfg.id)
        if tid in cfg.rule_set.template_ids:
            return cfg
        rs = cfg.rule_set.model_copy(update={"template_ids": [*cfg.rule_set.template_ids, tid]})
        return await self.update_device(cfg.model_copy(update={"rule_set": rs}))

    async def libraries_of(self, cfg: DeviceConfig, known: list[str] | None = None) -> list[str]:
        """The libraries the device pulls from: those of its templates, among the
        enabled libraries (or `known`)."""
        if known is None:
            known = [c.id for c in await self.store.libraries() if c.enabled]
        return device_libraries(cfg.rule_set, await self.store.templates(), known)

    async def erased(self, cfg: DeviceConfig) -> dict[str, Any] | None:
        """The device's last Wipe / Format when no full sync came after it, else None."""
        mark = cfg.erased
        if mark is None:
            return None
        full = await self.store.last_report(cfg.id, "full")
        if full is not None and full.started_at > mark.at:
            return None
        return mark.model_dump(mode="json")

    async def list_devices(self) -> dict[str, Any]:
        registered = []
        feedback = await self.store.feedback_counts()
        table = await asyncio.to_thread(mnt.mount_table)
        for cfg in await self.store.devices():
            libs = await self.libraries_of(cfg)
            cd = self.detector.by_device(cfg.id)
            fixed = bool(cfg.mount_path) and await asyncio.to_thread(
                Path(cfg.mount_path or "").is_dir
            )
            # configured = available; the server answers when it is used (an MTP player: while plugged in)
            network = self._network(cfg) and not self._usb_mtp(cfg)
            connected = cd is not None or fixed or network
            job = self.ctx.jobs.running_for_device(cfg.id)
            registered.append(
                {
                    "id": cfg.id,
                    "name": cfg.name,
                    "type": cfg.type,
                    "library_accounts": cfg.library_accounts,
                    "connected": connected,
                    "disk": cd.disk.path if cd else None,
                    # USB disk present without a medium / file system: storage not shared yet
                    "no_medium": cd is not None
                    and cd.data_partition is None
                    and not cfg.mount_path
                    and not self._usb_mtp(cfg),  # an MTP player has no partition
                    "mounted_at": str(self._mounts[cfg.id].path)
                    if cfg.id in self._mounts
                    else None,
                    # another installation is writing to it right now (its fresh lease)
                    "lease": await self._foreign_lease(cfg.id),
                    # mounted a second time by another program (the system's automount)
                    "foreign_mounts": self._foreign_in(cd, table)
                    if cd is not None and not cfg.mount_path
                    else [],
                    "busy": job.id if job else None,
                    # an integrity check is no sync: the UI says "Checking files…"
                    "busy_kind": job.kind if job else None,
                    "update": await update_status.update_state(self, cfg),  # the update line
                    "ejecting": cfg.id in self._ejecting,
                    # the clock is being set after a connect: not idle until the iPod settled
                    "setting_clock": self._setting_clock(cfg.id),
                    "gentle": cfg.sync.gentle_write,  # the Devices list's chip
                    # listening data not delivered to its library yet / given up on
                    "feedback_pending": feedback.get(cfg.id, (0, 0))[0],
                    "feedback_dropped": feedback.get(cfg.id, (0, 0))[1],
                    # "Storage may be failing": the dashboard's Needs you row
                    "health_warning": None if network else (await self.health(cfg.id))["warning"],
                    # ejected but its disk is not gone yet (powered off): not idle, nothing to ask it
                    "ejected": cfg.id in self._ejected,
                    # a damaged file system is an error too: the sidebar, dashboard and list show it
                    "error": (
                        self._errors.get(cfg.id)
                        or (
                            f"file system damaged ({self._damaged[cfg.id]}): check and repair it"
                            if cfg.id in self._damaged
                            else None
                        )
                    )
                    if connected
                    else None,
                    "fs_damaged": self._damaged.get(cfg.id) if connected else None,
                    # nothing to plan without a template or rule pulling from a library: not
                    # "waiting", the UI links to its Rules tab
                    "waiting": self._waiting.get(cfg.id) if connected and libs else None,
                    # outside its sync windows: when automatic syncs may start again
                    "window_wait": self._window_wait(cfg) if connected else None,
                    "no_libraries": not libs,
                    # wiped / formatted, not synced since
                    "erased": await self.erased(cfg),
                    "libraries": libs,
                    "template_ids": cfg.rule_set.template_ids,  # "used by N devices"
                    # a folder device cannot be formatted; bind it to its disk first
                    # an MTP player on USB has its own kind: plugged in, ejectable, no format
                    "kind": "usb-mtp"
                    if self._usb_mtp(cfg)
                    else "network"
                    if network
                    else "folder"
                    if cfg.mount_path
                    else "disk",
                    # its server did not answer the last automatic start
                    "offline_since": self._offline[cfg.id].isoformat()
                    if cfg.id in self._offline
                    else None,
                    "can_format": not cfg.mount_path and not self._network(cfg),
                    "bindable_disk": bd.disk.path
                    if (bd := await asyncio.to_thread(self._bindable_disk, cfg))
                    else None,
                }
            )
        unknown: list[dict[str, Any]] = []
        ignored: list[dict[str, Any]] = []
        for cd in self.detector.connected.values():
            if cd.device_id is not None:
                continue
            entry = {
                "disk": cd.disk.path,
                "model": cd.disk.model,
                "vendor": cd.disk.vendor,
                "size": cd.disk.size,
                "fstype": cd.disk.fstype,
                "serial": cd.disk.serial,
                "mtp": is_mtp(cd.disk),  # an Android player found on USB
                "probes": [p.model_dump(mode="json") for p in cd.probes],
            }
            # an ignored disk is left out of `unknown` (the dashboard, the list) until "Show ignored"
            (ignored if disk_key(cd.disk) in self._ignored else unknown).append(entry)
        return {"registered": registered, "unknown": unknown, "ignored": ignored}

    async def ignore_disk(self, disk: str, ignore: bool = True) -> None:
        """Ignore a plugged, unregistered disk (or stop ignoring it): remembered by its USB serial, so it stays
        ignored when plugged in again."""
        cd = next((c for c in self.detector.connected.values() if c.disk.path == disk), None)
        if cd is None or cd.device_id is not None:
            raise SyncError(f"no unregistered disk {disk!r} is plugged in", "not_found")
        key = disk_key(cd.disk)
        if ignore:
            self._ignored.add(key)
        else:
            self._ignored.discard(key)
        await self.store.put_ignored_disks(sorted(self._ignored))
        self.ctx.bus.publish("device.ignored" if ignore else "device.unignored", disk=disk)

    def _disk_mounted_at(self, path: Path) -> ConnectedDisk | None:
        """The connected disk one of whose file systems is mounted exactly at `path` (resolved), if any. Read
        from the kernel's mount table: a mount made after detection (desktop automount) sends no block uevent,
        so the detector's mountpoints can be stale."""
        source = mnt.mount_source(path)
        for cd in self.detector.connected.values():
            for n in (cd.disk, *cd.disk.children):
                if source in (n.path, f"/dev/{n.name}") or any(
                    Path(mp).resolve() == path for mp in n.mountpoints
                ):
                    return cd
        return None

    async def register(self, req: RegisterRequest) -> DeviceConfig:
        probe: ProbeResult | None = None
        path: Path | None = None
        if req.mount_path and not req.disk:
            # the root of a detected disk's mount (desktop automount): register the disk itself, so the
            # device is matched by USB serial / fs UUID and can be formatted
            resolved = await asyncio.to_thread(Path(req.mount_path).resolve)
            cd = await asyncio.to_thread(self._disk_mounted_at, resolved)
            if cd is not None and cd.device_id is None:
                log.info(
                    "register %s: %s is the mount of %s, registering the disk",
                    req.name,
                    resolved,
                    cd.disk.path,
                )
                req = req.model_copy(update={"disk": cd.disk.path, "mount_path": None})
        network = req.type in registry.DEVICES and registry.device_class(req.type).network
        if req.disk and req.type is None:  # a found MTP player: its type comes from the probe
            found = next(
                (c for c in self.detector.connected.values() if c.disk.path == req.disk), None
            )
            if found is not None and is_mtp(found.disk) and found.probes:
                req = req.model_copy(update={"type": found.probes[0].type_name})
                network = registry.device_class(found.probes[0].type_name).network
        usb_mtp = network and registry.device_class(req.type or "").usb_mtp
        if usb_mtp and req.disk:  # an MTP player found on USB: its serial comes from udev
            cd = next(
                (c for c in self.detector.connected.values() if c.disk.path == req.disk), None
            )
            if cd is None or not is_mtp(cd.disk):
                raise SyncError(f"no connected MTP player {req.disk}", "not_found")
            if cd.device_id is not None:
                raise SyncError(f"{req.disk} is already registered as {cd.device_id!r}", "conflict")
            probe = registry.device_class(req.type or "").probe(cd.disk, None)
            if probe is not None and probe.identity.usb_serial:
                req = req.model_copy(
                    # the serial is the identity: udev's, never one the form sent
                    update={"settings": {**req.settings, "usb_serial": probe.identity.usb_serial}}
                )
        elif network:  # an FTP server: its settings say where it is, no disk, no folder
            if req.disk or req.mount_path:
                raise SyncError(
                    f"a {req.type} device has no disk or folder: give its settings", "invalid"
                )
        elif req.disk:
            cd = next(
                (c for c in self.detector.connected.values() if c.disk.path == req.disk), None
            )
            if cd is None:
                raise SyncError(f"no connected disk {req.disk}", "not_found")
            if cd.device_id is not None:
                raise SyncError(f"{req.disk} is already registered as {cd.device_id!r}", "conflict")
            probe = next((p for p in cd.probes if req.type in (None, p.type_name)), None)
            path = cd.mountpoint
        elif req.mount_path:
            path = Path(req.mount_path)
            if not path.is_dir():
                raise SyncError(f"{path} is not a directory", "invalid")
            await self.check_mount_path(req.mount_path, None, req.type)  # before probing it
            for cls in registry.device_classes():
                if req.type in (None, cls.type_name):
                    probe = cls.probe(None, path)
                    if probe is not None or req.type is not None:
                        break
        else:
            raise SyncError(
                "register needs a connected disk, a mount_path, or a network device type", "invalid"
            )
        type_name = req.type or (probe.type_name if probe else None)
        if type_name is None:
            raise SyncError("no device class recognises this device; pass `type`", "invalid")
        cls = self._device_class(type_name)
        ids = {c.id for c in await self.store.devices()}
        dev_id = _slug(req.name)
        while dev_id in ids:
            dev_id = f"{_slug(req.name)}-{uuid.uuid4().hex[:4]}"
        rule_set = req.rule_set
        if req.library_ids:
            rule_set = rule_set.model_copy(
                update={
                    "template_ids": [
                        *rule_set.template_ids,
                        *await self._library_defaults(req.library_ids),
                    ]
                }
            )
        defaults = await self.store.app_settings()
        uid, on_connect, first_choice = uuid.uuid4().hex, req.on_connect, req.first_sync_choice
        continuity = req.continuity
        if continuity is None and path is not None:  # synced before: continue by default
            try:
                seen = await asyncio.to_thread(manifest_io.read_header, path)
            except (manifest_io.ManifestError, OSError):
                seen = None  # unreadable: registered as new, the plan asks about its files
            if seen is not None and seen.device_uid:
                continuity = "continue"
        if continuity is not None:
            header = await self._header_for_register(cls, type_name, path, req)
            if header is None or not header.device_uid:
                raise SyncError(
                    "nothing to continue: the device has no SyncMusic manifest", "invalid"
                )
            home = Manifest(header=header)
            if continuity == "continue":
                uid = header.device_uid
                if on_connect is None and not is_home(home):
                    on_connect = "manual"  # auto-sync on connect is the home's
            elif continuity == "fresh":
                if not is_home(home):
                    raise SyncError(
                        f"only the home instance {home_name(home)} can start the device fresh: "
                        "continue, then make this instance home",
                        "not_home",
                    )
                first_choice = "wipe"
            else:
                first_choice = first_choice or "keep"
        cfg = DeviceConfig(
            id=dev_id,
            type=type_name,
            name=req.name,
            identity=(
                probe.identity if probe else _settings_identity(cls, req.settings)
            ).model_copy(update={"manifest_device_id": uid}),
            rule_set=rule_set,
            on_connect=on_connect or defaults.default_on_connect,
            sync_windows=defaults.default_sync_windows,
            update_sync=defaults.default_update_sync,  # Settings → Sync defaults
            # A server is not plugged in, so it has no on-connect sync: every 6 hours
            update_every_min=NETWORK_UPDATE_EVERY_MIN
            if network and not usb_mtp  # an MTP player is plugged in: it has its on-connect sync
            else defaults.default_update_every_min,
            sync=SyncSettings(
                verify=defaults.default_verify,
                eject_after=defaults.default_eject_after,
                no_cache=defaults.default_no_cache,
                no_cache_files=defaults.default_no_cache_files,
                no_cache_budget_mb=defaults.default_no_cache_budget_mb,
                no_cache_file_max_mb=defaults.default_no_cache_file_max_mb,
                combine_audiobooks=type_name in COMBINE_AUDIOBOOKS_TYPES,
            ),
            settings=dict(req.settings),
            mount_path=req.mount_path,
            first_sync_choice=first_choice,
        )
        if "time_sync" in cls.Settings.model_fields and "time_sync" not in cfg.settings:
            # new devices set their clock; a stored config without it keeps the old "off"
            cfg.settings["time_sync"] = True
        cls.Settings.model_validate(cfg.settings)  # needed by the constructor below
        template_id = req.processor_template_id
        if template_id is None and req.processors is None:
            template_id = DEFAULT_PROCESSOR_TEMPLATES.get(type_name)
        if template_id:
            tpl = await self.store.processor_template(template_id)
            if tpl is None:
                raise SyncError(f"unknown processor template {template_id!r}", "not_found")
            cfg.processors = [p.model_copy(deep=True) for p in tpl.processors]
            cfg.processor_template_id = tpl.id
        elif req.processors is None:
            device = cls(cfg, path or Path("/nonexistent"), self.ctx)
            cfg.processors = suggested_processors(device.effective_capabilities())
        else:
            cfg.processors = list(req.processors)
        cfg.processors = with_tag_step(cfg.processors)
        await self.validate(cfg)
        if network:  # log in once: wrong host or password is said now, not at the first sync
            await self.check_network(cfg)
        await self.store.put_device(cfg)
        await self.load()
        if req.disk:
            cd = next(c for c in self.detector.connected.values() if c.disk.path == req.disk)
            await self.detector.disk_added(cd.disk)  # re-match: now registered
        self.ctx.bus.publish("device.registered", device_id=cfg.id, type=cfg.type)
        return cfg

    async def peek_manifest(self, disk: str | None, mount_path: str | None) -> dict[str, Any]:
        """Register's question before the form is sent: was this device synced by SyncMusic, and whose is it."""
        path: Path | None = None
        if disk:
            cd = next((c for c in self.detector.connected.values() if c.disk.path == disk), None)
            path = cd.mountpoint if cd is not None else None
        elif mount_path:
            path = Path(mount_path)
        header = None
        if path is not None:
            try:
                header = await asyncio.to_thread(manifest_io.read_header, path)
            except (manifest_io.ManifestError, OSError) as e:
                log.info("peek %s: manifest unreadable: %s", path, e)
        if header is None or not header.device_uid:
            return {"synced_before": False, "home": None, "last_writer": None, "is_home": True}
        m = Manifest(header=header)
        return {
            "synced_before": True,
            "home": header.home.model_dump(mode="json") if header.home else None,
            "last_writer": header.last_writer.model_dump(mode="json")
            if header.last_writer
            else None,
            "is_home": is_home(m),
        }

    async def _header_for_register(
        self, cls: type[Device], type_name: str, path: Path | None, req: RegisterRequest
    ) -> ManifestHeader | None:
        """The manifest header of a device being registered: from its mount, else read through the device."""
        if path is not None:
            return await asyncio.to_thread(manifest_io.read_header, path)
        probe_cfg = DeviceConfig(
            id="register-probe", type=type_name, name=req.name, settings=dict(req.settings)
        )
        try:
            manifest = await cls(probe_cfg, Path("/nonexistent"), self.ctx).read_manifest()
        except Exception as e:  # noqa: BLE001 - unreadable: there is nothing to continue
            log.info("register %s: manifest not read: %s", req.name, e)
            return None
        return manifest.header if manifest is not None else None

    async def _require_home(self, cfg: DeviceConfig, action: str, root: Path | None = None) -> None:
        """Refuse a home-only action (wipe, format, removing unknown files) on another instance's device; `root`:
        read the manifest header from this mounted root instead of opening the device."""
        try:
            if root is not None:
                header = await asyncio.to_thread(manifest_io.read_header, root)
                manifest = Manifest(header=header) if header is not None else None
            else:
                manifest = await (await self.open_device(cfg.id)).read_manifest()
        except Exception as e:  # noqa: BLE001 - no readable manifest: nobody else claims the device
            log.info("%s: home check without a manifest: %s", cfg.id, e)
            return
        if not is_home(manifest):
            raise SyncError(
                f"only the home instance {home_name(manifest)} can {action} {cfg.name}: "
                "make this instance home first",
                "not_home",
            )

    async def home_info(self, device_id: str) -> dict[str, Any]:
        """The home instance and the last writer from the manifest (none before the first sync)."""
        cfg = await self.config(device_id)
        manifest = await (await self.open_device(cfg.id)).read_manifest()
        h = manifest.header if manifest is not None else None
        return {
            "home": h.home.model_dump(mode="json") if h and h.home else None,
            "last_writer": h.last_writer.model_dump(mode="json") if h and h.last_writer else None,
            "is_home": is_home(manifest),
            "instance_id": manifest_io.context().instance_id,
        }

    async def _home_of_mounted(self, device_id: str) -> bool:
        """This instance is home of the device as far as its mounted manifest says (not mounted: assumed)."""
        held = self._mounts.get(device_id)
        if held is None:
            return True
        try:
            header = await asyncio.to_thread(manifest_io.read_header, held.path)
        except (manifest_io.ManifestError, OSError):
            return True
        return header is None or is_home(Manifest(header=header))

    async def make_home(self, device_id: str) -> InstanceInfo:
        """Re-bind: this instance becomes the device's home (written into the manifest header)."""
        cfg = await self.config(device_id)
        if self.ctx.jobs.device_busy(cfg.id):
            raise SyncError(f"{cfg.name} is busy", "busy")
        device = await self.open_device(cfg.id)
        manifest = await device.read_manifest()
        if manifest is None:
            raise SyncError(f"{cfg.name} was never synced: the first sync makes it home", "invalid")
        if foreign_manifest(manifest, cfg):
            raise SyncError(f"{cfg.name} belongs to another registration", "invalid")
        ctx = manifest_io.context()
        if not ctx.instance_id:
            raise SyncError("this instance has no id", "invalid")
        home = InstanceInfo(id=ctx.instance_id, name=ctx.instance_name, since=utcnow())
        manifest.header = manifest.header.model_copy(update={"home": home})
        await device.write_manifest(manifest)
        self.drop_plans(cfg.id)
        self.ctx.bus.publish("device.updated", device_id=cfg.id)
        return home

    def _bindable_disk(self, cfg: DeviceConfig) -> ConnectedDisk | None:
        """For a directory-bound device: the connected disk it lives on (matched by its manifest, or an
        unknown disk mounted exactly at its directory) - it can be bound to that disk."""
        if not cfg.mount_path:
            return None
        cd = self.detector.by_device(cfg.id)
        if cd is not None:
            return cd
        cd = self._disk_mounted_at(Path(cfg.mount_path).resolve())
        return cd if cd is not None and cd.device_id is None else None

    async def bind_disk(self, device_id: str, disk: str | None = None) -> DeviceConfig:
        """Turn a directory-bound device into a disk device: its identity becomes the disk's
        USB serial / fs UUID (the manifest id is kept, so the files on it stay this device's), the directory
        is dropped and the device is mounted by SyncMusic from now on - Format becomes possible."""
        cfg = await self.config(device_id)
        if not cfg.mount_path:
            raise SyncError(f"{cfg.name} is already bound to a disk", "invalid")
        if self.ctx.jobs.device_busy(device_id):
            raise SyncError(f"{cfg.name} is busy", "busy")
        cd = await asyncio.to_thread(self._bindable_disk, cfg)
        if disk is not None and (cd is None or cd.disk.path != disk):
            cd = next((c for c in self.detector.connected.values() if c.disk.path == disk), None)
            if cd is None:
                raise SyncError(f"no connected disk {disk}", "not_found")
            if cd.device_id not in (None, cfg.id):
                raise SyncError(f"{disk} is already registered as {cd.device_id!r}", "conflict")
        if cd is None:
            raise SyncError(
                f"{cfg.name}: no connected disk found for {cfg.mount_path}", "not_connected"
            )
        probe = self._device_class(cfg.type).probe(cd.disk, cd.mountpoint)
        if probe is None:
            raise SyncError(f"{cd.disk.path} is not a {cfg.type} device", "invalid")
        cfg.identity = probe.identity.model_copy(
            update={"manifest_device_id": cfg.identity.manifest_device_id}
        )
        cfg.mount_path = None
        await self.store.put_device(cfg)
        self.drop_plans(cfg.id)
        await self.load()
        await self.detector.rematch()
        await self.detector.disk_added(cd.disk)
        self.ctx.bus.publish("device.updated", device_id=cfg.id)
        return cfg

    async def _check_music_only(self, cfg: DeviceConfig) -> None:
        """A music-only device (Rockbox): no capability override turns podcasts or audiobooks on, and
        no linked template draws only from libraries without music (Audiobookshelf)."""
        cls = self._device_class(cfg.type)
        if any(
            cfg.capability_overrides.get(k) for k in ("supports_podcasts", "supports_audiobooks")
        ):
            raise SyncError(
                f"a {cls.display_name} takes music only: no podcasts or audiobooks", "invalid"
            )
        templates = await self.store.templates()
        libraries = {lib.id: lib for lib in await self.store.libraries()}
        for tid in cfg.rule_set.template_ids:
            tpl = templates.get(tid)
            if tpl is None:
                continue
            libs = [libraries[x] for x in tpl.library_ids if x in libraries]
            if libs and not any(
                MediaKind.MUSIC in registry.library_class(lib.type).capabilities.kinds
                for lib in libs
            ):
                names = ", ".join(lib.name for lib in libs)
                raise SyncError(
                    f"template {tpl.name!r} draws only podcasts and audiobooks ({names}); "
                    f"a {cls.display_name} takes music only",
                    "invalid",
                )

    @staticmethod
    def _device_class(type_name: str) -> type[Device]:
        try:
            return registry.device_class(type_name)
        except KeyError as e:
            raise SyncError(str(e.args[0]), "invalid") from None  # names the known types

    async def check_mount_path(
        self, mount_path: str, device_id: str | None, type_name: str | None
    ) -> None:
        """Refuse a device directory that syncing could damage (see core/mount_path.py):
        SyncError("invalid") for protected or non-removable paths, ("conflict") when it overlaps
        another device's directory. An iPod / Rockbox type may use the root of its tree;
        `type_name` None (probe later) gets the strict check."""
        owns_tree = False
        if type_name is not None:
            owns_tree = self._device_class(type_name).owns_device_tree
        path = await asyncio.to_thread(Path(mount_path).resolve)
        conf = self.ctx.config
        protected = [
            ("the app data directory", conf.data_dir),
            ("the app cache directory", conf.cache_dir),
            ("the app mount directory", conf.mount_root),
            ("the app installation", Path(__file__).parents[2]),
        ]
        for lib in await self.store.libraries():
            root = lib.settings.get("root")
            if isinstance(root, str) and root:
                protected.append((f"library {lib.id!r}", Path(root)))
        resolved = [(w, await asyncio.to_thread(p.resolve)) for w, p in protected]
        for other in await self.store.devices():
            if other.id == device_id or not other.mount_path:
                continue
            if overlaps(path, await asyncio.to_thread(Path(other.mount_path).resolve)):
                raise SyncError(
                    f"{mount_path} is already used by device {other.id!r} ({other.mount_path})",
                    "conflict",
                )
        reason = protected_reason(path, resolved) or await asyncio.to_thread(
            device_tree_reason, path, owns_tree
        )
        if reason is not None:
            raise SyncError(f"refusing {mount_path} as a device directory: {reason}", "invalid")
        if not conf.allow_directory_devices and not await self._removable(path):
            raise SyncError(
                f"refusing {mount_path} as a device directory: it is not on a removable disk",
                "invalid",
            )

    async def check_library_root(self, root: str) -> None:
        """Refuse a folder library root that overlaps a device directory (SyncError "conflict") or the
        app's data/cache/mount dirs ("invalid"): check_mount_path would then refuse the device on every
        plan and edit."""
        path = await asyncio.to_thread(Path(root).resolve)
        for dev in await self.store.devices():
            if dev.mount_path and overlaps(
                path, await asyncio.to_thread(Path(dev.mount_path).resolve)
            ):
                raise SyncError(
                    f"{root} overlaps the directory of device {dev.id!r} ({dev.mount_path})",
                    "conflict",
                )
        conf = self.ctx.config
        for what, p in (
            ("the app data directory", conf.data_dir),
            ("the app cache directory", conf.cache_dir),
            ("the app mount directory", conf.mount_root),
        ):
            if overlaps(path, await asyncio.to_thread(p.resolve)):
                raise SyncError(f"{root} overlaps {what} ({p})", "invalid")

    async def _removable(self, path: Path) -> bool:
        return await asyncio.to_thread(on_removable_disk, path, self.ctx.guard)

    async def validate(self, cfg: DeviceConfig) -> None:
        """Reject a config that could not be planned: unknown type, processor or library,
        bad settings, invalid rules, a dangerous mount_path. Raises SyncError("invalid" /
        "conflict") or pydantic's ValidationError."""
        devices = await self.store.devices()
        old = next((d for d in devices if d.id == cfg.id), None)
        same = next(
            (
                d
                for d in devices
                if d.id != cfg.id and d.name.strip().casefold() == cfg.name.strip().casefold()
            ),
            None,
        )
        # rows in the UI would be indistinguishable; a name stored before that is kept until renamed
        if same is not None and (old is None or old.name != cfg.name):
            raise SyncError(
                f"a device named {same.name!r} already exists (id {same.id})", "invalid"
            )
        if cfg.mount_path:
            await self.check_mount_path(cfg.mount_path, cfg.id, cfg.type)
        self._device_class(cfg.type).Settings.model_validate(cfg.settings)
        # unknown keys and wrong values are refused (the profile card's overrides)
        self._device_class(cfg.type).capabilities.with_overrides(cfg.capability_overrides)
        for slot in cfg.processors:
            try:
                registry.processor_class(slot.type).Settings.model_validate(slot.settings)
            except KeyError as e:
                raise SyncError(str(e.args[0]), "invalid") from None
        if cfg.rule_set.rules or cfg.rule_set.excludes or cfg.library_ids:
            raise SyncError(
                "a device has templates only: put its rules into a template and link that",
                "invalid",
            )
        if self._device_class(cfg.type).music_only:
            await self._check_music_only(cfg)
        if cfg.processor_template_id and not await self.store.processor_template(
            cfg.processor_template_id
        ):
            raise SyncError(f"unknown processor template {cfg.processor_template_id!r}", "invalid")
        result = validate_ruleset(cfg.rule_set)
        if not result.ok:
            raise SyncError("invalid rules: " + "; ".join(result.errors), "invalid")

    async def update_device(self, cfg: DeviceConfig) -> DeviceConfig:
        before = await self.config(cfg.id)
        cfg = cfg.model_copy(update={"processors": with_tag_step(cfg.processors)})
        await self.validate(cfg)
        await self.store.put_device(cfg)
        self._configs = await self.store.devices()  # a device change needs no library work (load())
        if (before.identity, before.type, before.mount_path) != (
            cfg.identity,
            cfg.type,
            cfg.mount_path,
        ):
            await self.detector.rematch()  # only what matching a disk uses
        if _plan_shape(before) != _plan_shape(cfg):  # how it is applied only: the plan stays
            self.drop_plans(cfg.id)
        if self.detector.by_device(cfg.id) is None and not self.ctx.jobs.device_busy(cfg.id):
            await self._unmount(cfg.id)  # no longer matches its disk: drop its mount
        # the sidebar card and lists follow (e.g. "no libraries")
        self.ctx.bus.publish("device.updated", device_id=cfg.id)
        return cfg

    async def delete_device(self, device_id: str) -> None:
        await self.config(device_id)
        if self.ctx.jobs.device_busy(device_id):
            raise SyncError(f"{device_id} is busy", "busy")
        await self._unmount(device_id)
        await self._close_mtp(device_id)
        self.drop_plans(device_id)
        await self.store.delete_device(device_id)
        self._device_items.pop(device_id, None)
        own = own_template_id(
            device_id
        )  # its own template goes with it unless another device links it too
        if not any(own in d.rule_set.template_ids for d in await self.store.devices()):
            await self.store.delete_template(own)
        await self.load()
        await self.detector.rematch()  # its disk is unknown again

    async def open_device(self, device_id: str) -> Device:
        """Device instance on its mounted tree (mounts the data partition if needed)."""
        cfg = await self.config(device_id)
        if device_id in self._ejecting:
            raise SyncError(f"{cfg.name} is being ejected", "busy")
        if device_id in self._ejected:
            raise SyncError(
                f"{cfg.name} was ejected: unplug it and connect it again", "not_connected"
            )
        cls = registry.device_class(cfg.type)
        if cls.usb_mtp and self.detector.by_device(device_id) is None:
            raise SyncError(
                f"{cfg.name} is not connected: plug it in, unlock it and choose 'File transfer' in its USB menu",
                "not_connected",
            )
        if cls.network:
            await asyncio.to_thread(self._network_dir(cfg).mkdir, parents=True, exist_ok=True)
            if not cls.usb_mtp:
                return cls(cfg, self._network_dir(cfg), self.ctx)
            open_ = self._mtp_open.get(device_id)
            if open_ is not None and open_.config == cfg:
                return open_
            await self._close_mtp(device_id)
            device = self._mtp_open[device_id] = cls(cfg, self._network_dir(cfg), self.ctx)
            return device
        if cfg.mount_path:
            path = Path(cfg.mount_path)
            if not await asyncio.to_thread(path.is_dir):
                raise SyncError(f"{cfg.name}: {path} is not available", "not_connected")
            return cls(cfg, path, self.ctx)
        cd = self.detector.by_device(device_id)
        if cd is None:
            raise SyncError(f"{cfg.name} is not connected", "not_connected")
        if cd.data_partition is None:
            # the USB disk is there without a medium or file system yet (a player that has not
            # shared its storage, or a disk still being identified)
            raise SyncError(f"{cfg.name}: {NO_MEDIUM_HINT}", "no_medium")
        # one opener at a time per device: requests that waited for the clock settle (or simply arrived together)
        # would otherwise each mount the partition, stacking mounts the unmount removes only one of
        async with self._mount_locks.setdefault(device_id, asyncio.Lock()):
            m = self._mounts.get(device_id)
            if m is not None and not await asyncio.to_thread(mnt.is_live, m):
                # unmounted behind our back (by hand, desktop): never use the bare mountpoint
                log.warning("%s: mount at %s is gone, mounting again", device_id, m.path)
                self._mounts.pop(device_id, None)
                m = None
            if m is None:
                if self.ctx.guard is None:
                    raise SyncError("no block device guard configured", "invalid")
                if device_id in self._ejecting or device_id in self._ejected:
                    raise SyncError(f"{cfg.name} is being ejected", "busy")
                clock = self._clock_tasks.get(device_id)
                if clock is not None and not clock.done():
                    await asyncio.shield(
                        clock
                    )  # the iPod writes its FAT after the clock: never mount meanwhile
                registered = cfg.identity.guard_ids()
                try:
                    m = await mnt.mount(
                        self.ctx.guard,
                        cd.data_partition,
                        device_id,
                        self.ctx.config.mount_root,
                        registered=registered,
                    )
                except mnt.UnmountableError as e:
                    self._errors[device_id] = str(e)
                    raise SyncError(f"{cfg.name}: {e}", "unmountable") from e
                self._errors.pop(device_id, None)
                self._mounts[device_id] = m
                # the sidebar shows the used space of mounted devices only
                self.ctx.bus.publish("device.mounted", device_id=device_id)
                if cfg.sync.unmount_foreign:
                    self._watch_foreign(cfg)
        dev = cls(cfg, m.path, self.ctx)
        dev.block_path = cd.disk.path
        return dev

    def _mount_check(self, cfg: DeviceConfig) -> Callable[[], Awaitable[None]] | None:
        """Raises DeviceLostError once the mount the sync started on is gone: from then on
        the mountpoint is a bare directory on the system disk / tmpfs and must not be written."""
        m = self._mounts.get(cfg.id)
        if m is None:
            return None

        async def check() -> None:
            if not await asyncio.to_thread(mnt.is_live, m):
                raise DeviceLostError(
                    f"device {cfg.name} was unmounted during the sync ({m.dev_path} at {m.path})"
                )

        return check

    def _shared(self, m: mnt.Mounted) -> str | None:
        """Another device whose cached mount is the same mountpoint as `m`."""
        return next((d for d, o in self._mounts.items() if o.path == m.path), None)

    async def _unmount(self, device_id: str) -> None:
        m = self._mounts.pop(device_id, None)
        if m is not None and (other := self._shared(m)) is not None:
            # another device reuses this mountpoint (same disk matched twice): leave it mounted
            # and hand the ownership over instead of pulling the file system away
            if m.owned:
                self._mounts[other] = dataclasses.replace(self._mounts[other], owned=True)
            return
        if m is not None:
            try:
                await mnt.unmount(m)
            except mnt.BlockdevError as e:
                log.warning("unmount %s: %s", device_id, e)

    async def _foreign_lease(self, device_id: str) -> dict[str, Any] | None:
        """Another installation's fresh lease on a mounted device ({name, since}), else None. Mounted devices only:
        the list never opens a device for it."""
        m = self._mounts.get(device_id)
        if m is None:
            return None
        path = m.path / manifest_io.STATE_DIR / device_lease.FILE_NAME
        try:
            raw = await asyncio.to_thread(path.read_bytes)
        except OSError:
            return None
        held = device_lease.parse(raw)
        if held is None or not device_lease.blocking(held, manifest_io.context().instance_id):
            return None
        return {"name": held.name or held.instance_id, "since": held.started_at.isoformat()}

    def _foreign_in(self, cd: ConnectedDisk, table: list[mnt.MountEntry]) -> list[str]:
        """Mountpoints of the disk's partitions in `table` that the app did not make."""
        parts = {n.path for n in (cd.disk, *cd.disk.children)}
        ours = {str(m.path) for m in self._mounts.values()}
        root = str(self.ctx.config.mount_root.resolve())
        return [
            e.path
            for e in table
            if e.source in parts and e.path not in ours and str(Path(e.path).parent) != root
        ]

    # a host automounter mounts a new disk some seconds after it appears: look again a few times
    FOREIGN_CHECKS_S = (2.0, 15.0, 30.0, 60.0)

    def _watch_foreign(self, cfg: DeviceConfig) -> None:
        """`unmount_foreign`: unmount other programs' mounts of the device for a minute after the app mounted it
        (busy ones are left alone and logged)."""
        old = self._foreign_tasks.pop(cfg.id, None)
        if old is not None and not old.done():
            old.cancel()

        async def run() -> None:
            for delay in self.FOREIGN_CHECKS_S:
                await asyncio.sleep(delay)
                cd = self.detector.by_device(cfg.id)
                if cd is None or self.ctx.guard is None or cfg.id in self._ejecting:
                    return
                table = await asyncio.to_thread(mnt.mount_table)
                for path in self._foreign_in(cd, table):
                    try:
                        await asyncio.to_thread(
                            self.ctx.guard.check, cd.disk.path, "eject", cfg.identity.guard_ids()
                        )
                        await mnt.run_cmd(["umount", path])
                        log.info("%s: unmounted another program's mount at %s", cfg.name, path)
                    except (GuardError, mnt.BlockdevError) as e:
                        log.warning("%s: could not unmount %s: %s", cfg.name, path, e)

        self._foreign_tasks[cfg.id] = asyncio.create_task(run(), name=f"foreign-mounts-{cfg.id}")

    def _disk_of(self, cfg: DeviceConfig) -> tuple[Guard, BlockDeviceInfo]:
        if cfg.mount_path or self._network(cfg):
            raise SyncError(f"{cfg.name} is not a disk: nothing to unmount", "invalid")
        cd = self.detector.by_device(cfg.id)
        if cd is None:
            raise SyncError(f"{cfg.name} is not connected", "not_connected")
        if self.ctx.guard is None:
            raise SyncError("no block device guard configured", "invalid")
        try:
            disk, _ = self.ctx.guard.check(cd.disk.path, "eject", cfg.identity.guard_ids())
        except (GuardError, mnt.BlockdevError) as e:
            raise SyncError(f"{cfg.name}: {e}", "invalid") from e
        return self.ctx.guard, disk

    async def mounts(self, device_id: str) -> list[dict[str, Any]]:
        """Every mountpoint of the device's partitions: the app's own and other programs' (host automounters)."""
        cfg = await self.config(device_id)
        _, disk = await asyncio.to_thread(self._disk_of, cfg)
        return [dataclasses.asdict(m) for m in mnt.disk_mounts(disk, self.ctx.config.mount_root)]

    async def foreign_mounts(self, cfg: DeviceConfig) -> list[str]:
        """Mountpoints of the device that other programs made (not the app's), [] for non-disks."""
        try:
            _, disk = await asyncio.to_thread(self._disk_of, cfg)
        except SyncError:
            return []
        ours = {str(m.path) for m in self._mounts.values()}
        return [
            m.path
            for m in mnt.disk_mounts(disk, self.ctx.config.mount_root)
            if not m.ours and m.path not in ours
        ]

    async def unmount_everywhere(
        self, device_id: str, *, lazy: bool = False
    ) -> list[dict[str, Any]]:
        """Flush and unmount every mount of the device, also those of other programs, so Repair, Format or
        Wipe can run. A busy mount fails with code `mount_busy` naming the holders; `lazy` detaches it."""
        cfg = await self.config(device_id)
        guard, disk = await asyncio.to_thread(self._disk_of, cfg)
        await self._yield_update(cfg.id)
        lock = self.ctx.jobs.device_lock(device_id)
        if lock.locked() or self.ctx.jobs.device_busy(cfg.id):
            raise SyncError(f"{cfg.name} is busy", "busy")
        async with lock:
            await self._unmount(cfg.id)
            self.drop_plans(cfg.id)
            try:
                done = await mnt.unmount_everywhere(
                    guard,
                    disk.path,
                    cfg.identity.guard_ids(),
                    lazy=lazy,
                    mount_root=self.ctx.config.mount_root,
                )
            except mnt.BusyMountError as e:
                raise SyncError(str(e), "mount_busy") from e
            except mnt.BlockdevError as e:
                raise SyncError(f"{cfg.name}: {e}", "sync_error") from e
        log.info("%s: unmounted everywhere: %s", cfg.name, ", ".join(m.path for m in done) or "-")
        self.ctx.bus.publish("device.unmounted", device_id=cfg.id)
        return [dataclasses.asdict(m) for m in done]

    async def eject(self, device_id: str) -> None:
        """Unmount, set the clock (iPod, opt-in), power off. From the first step to the last the device is
        busy and never mounted again: after a time sync the iPod writes its own FAT."""
        cfg = await self.config(device_id)
        if self._network(cfg) and not self._usb_mtp(cfg):
            raise SyncError(f"{cfg.name} is a server: nothing to eject", "invalid")
        lock = self.ctx.jobs.device_lock(device_id)
        if lock.locked() or device_id in self._ejecting:
            raise SyncError(f"{cfg.name} is busy", "busy")
        self._ejecting.add(device_id)
        try:
            async with lock:
                await self._eject(cfg)
            if self.detector.by_device(device_id) is not None:
                self._ejected.add(device_id)  # until udev reports the disk gone
        finally:
            self._ejecting.discard(device_id)
        self.ctx.bus.publish("device.ejected", device_id=device_id)

    async def _close_mtp(self, device_id: str) -> None:
        device = self._mtp_open.pop(device_id, None)
        if device is not None:
            try:
                await device.close()
            except Exception as e:  # noqa: BLE001 - a vanished player cannot be released cleanly
                log.warning("closing the MTP session of %s: %s", device_id, e)

    async def _eject(self, cfg: DeviceConfig) -> None:
        if self._usb_mtp(cfg):  # release the MTP session: nothing to unmount or power off
            self.drop_plans(cfg.id)
            await self._close_mtp(cfg.id)
            return
        cd = self.detector.by_device(cfg.id)
        await self._unmount(cfg.id)
        self.drop_plans(cfg.id)
        if cd is not None and self.ctx.guard is not None:
            device = registry.device_class(cfg.type)(
                cfg, self.ctx.config.mount_root / cfg.id, self.ctx
            )
            device.block_path = cd.disk.path
            if self._clock_due(cfg.id) and await self._sync_time(cfg, device):
                self._clock_set[cfg.id] = time.monotonic()
                # let the iPod finish writing its Device files; tell the UI why eject takes a while
                self.ctx.bus.publish("device.ejecting", device_id=cfg.id, settle_s=scsi.SETTLE_S)
                await self._settle(cd.disk.path, cd.data_partition)
            registered = cfg.identity.guard_ids()
            await mnt.eject(self.ctx.guard, cd.disk.path, registered=registered)

    async def format(self, device_id: str, confirm: str, fs: str | None = None) -> DeviceConfig:
        cfg = await self.config(device_id)
        if confirm != cfg.name:
            raise SyncError("format needs confirm = the device's name", "confirmation_required")
        if cfg.mount_path:
            raise SyncError(f"{cfg.name} is a directory, not a disk: nothing to format", "invalid")
        if self._network(cfg):
            raise SyncError(f"{cfg.name} is a server: nothing to format", "invalid")
        if self.ctx.jobs.device_busy(device_id):
            raise SyncError(f"{cfg.name} is busy", "busy")
        cd = self.detector.by_device(device_id)
        if cd is None:
            raise SyncError(f"{cfg.name} is not connected", "not_connected")
        # never a mount for the check: the old file system may not mount at all
        held = self._mounts.get(device_id)
        if held is not None:
            await self._require_home(cfg, "format", held.path)
        # no mount first: the old file system may not mount at all (dirty NTFS, corrupt)
        await self._unmount(device_id)
        self.drop_plans(device_id)
        device = registry.device_class(cfg.type)(
            cfg, self.ctx.config.mount_root / device_id, self.ctx
        )
        device.block_path = cd.disk.path
        try:
            part = await device.format(cfg.name, fs)
        except NotImplementedError as e:
            raise SyncError(str(e), "invalid") from e
        # format mounted the new partition at our mountpoint: record that partition (not the disk),
        # or the liveness check calls the mount gone and it is never unmounted
        part = part or cd.data_partition or cd.disk.path
        cd.data_partition = part
        self._mounts[device_id] = mnt.Mounted(device.mount, part, fs or "vfat", owned=True)
        self._errors.pop(device_id, None)
        self._damaged.pop(device_id, None)
        cfg = device.config.model_copy(update={"erased": ErasedMark(how="formatted")})
        await self.store.put_device(cfg)
        await self.load()
        self.ctx.bus.publish("device.formatted", device_id=device_id, fs=fs)
        return cfg

    async def wipe(self, device_id: str, confirm: str) -> JobInfo:
        """Delete everything SyncMusic and others put on a Rockbox or HiBy player, as a job (Device settings' Wipe): `Device.wipe` keeps the player's own folder (`/.rockbox`: it still
        boots; HiBy: `/.local` databases) and empties our state folder, so the next sync is a first sync. Other types
        format instead (`format`)."""
        cfg = await self.config(device_id)
        if confirm != cfg.name:
            raise SyncError("wipe needs confirm = the device's name", "confirmation_required")
        if cfg.type not in WIPE_KEEPS:
            raise SyncError(
                f"{cfg.name} is not a Rockbox, HiBy or Android player: format it instead", "invalid"
            )
        kept = WIPE_KEEPS[cfg.type]
        await self._yield_update(cfg.id)  # a user action wins over an update sync
        if self.ctx.jobs.device_busy(cfg.id):
            raise SyncError(f"{cfg.name} is busy", "busy")
        await self.open_device(cfg.id)  # not connected / unmountable: refused now, not in the job
        await self._require_home(cfg, "wipe")

        async def job(ctx: JobContext) -> dict[str, Any]:
            device = await self.open_device(cfg.id)
            self.drop_plans(cfg.id)
            ctx.progress(0.0, f"wiping {cfg.name}: everything but {kept}")
            removed = await device.wipe()
            ctx.progress(1.0, f"{removed} files removed, {kept} kept")
            self._device_items.pop(cfg.id, None)
            self._damaged.pop(cfg.id, None)
            # the device pages say "wiped" instead of the last sync's "synced" until the next full sync
            fresh = await self.config(cfg.id)
            mark = ErasedMark(how="wiped", removed=removed)
            await self.store.put_device(fresh.model_copy(update={"erased": mark}))
            await self.load()
            self.ctx.bus.publish("device.wiped", device_id=cfg.id)
            # the job view says how many files went and what stayed
            return {"removed": removed, "type": cfg.type}

        return self.ctx.jobs.submit(
            "wipe", f"wipe {cfg.name}", self._leased(cfg, job), device_id=cfg.id
        )

    def _rename_target(self, cfg: DeviceConfig) -> tuple[str, BlockDeviceInfo] | None:
        """(data partition, its node from a fresh scan) of a connected disk device, else None."""
        cd = None if cfg.mount_path else self.detector.by_device(cfg.id)
        if cd is None or cd.data_partition is None or self.ctx.guard is None:
            return None
        try:
            _, node = find_disk_of(cd.data_partition, scan_block_devices(False))
        except KeyError:
            raise SyncError(f"{cfg.name} is not connected", "not_connected") from None
        return cd.data_partition, node

    async def rename_preview(self, device_id: str, name: str) -> dict[str, Any]:
        """What a Rename to `name` writes, without writing: the name and the file system label."""
        cfg = await self.config(device_id)
        name = " ".join(name.split())
        target = await asyncio.to_thread(self._rename_target, cfg)
        if target is None:
            return {"name": name, "label": None, "fs": None, "current_label": None}
        node = target[1]
        device = registry.device_class(cfg.type)(cfg, self.ctx.config.mount_root / cfg.id, self.ctx)
        label = device.label_for(name, node.fstype, node.label) if name else None
        return {"name": name, "label": label, "fs": node.fstype, "current_label": node.label}

    async def rename(self, device_id: str, name: str) -> tuple[DeviceConfig, str | None]:
        """Rename the device on the device itself: a device action like Format, not a sync step.
        The device writes the name it keeps in its files (the iPod's database), then the data partition's file
        system label is changed (`Device.label_for`; unmounted for that, mounted again when next used), and the
        display name in SyncMusic follows. Returns (config, label written or None)."""
        cfg = await self.config(device_id)
        name = " ".join(name.split())
        if not name:
            raise SyncError("the new name is empty", "invalid")
        if len(name) > NAME_MAX:
            raise SyncError(f"the name is longer than {NAME_MAX} characters", "invalid")
        lock = self.ctx.jobs.device_lock(device_id)
        if (
            lock.locked()
            or device_id in self._ejecting
            or self.ctx.jobs.running_for_device(device_id) is not None
        ):
            raise SyncError(f"{cfg.name} is busy", "busy")
        async with lock:
            device = await self.open_device(device_id)
            await device.rename(name)
            label = None
            target = await asyncio.to_thread(self._rename_target, cfg)
            if target is not None and self.ctx.guard is not None:
                part, node = target
                label = device.label_for(name, node.fstype, node.label)
                if label is not None and label != node.label:
                    await self._unmount(device_id)
                    registered = cfg.identity.guard_ids()
                    label = await fmt.set_label(
                        self.ctx.guard, part, str(node.fstype), label, registered
                    )
            cfg = (await self.config(device_id)).model_copy(update={"name": name})
            await self.store.put_device(cfg)
            await self.load()
            self.drop_plans(device_id)
        self.ctx.bus.publish("device.renamed", device_id=device_id, name=name, label=label)
        return cfg, label

    async def play_log(self, device_id: str) -> dict[str, Any]:
        """Rockbox play log: the device setting and, when the player is connected, its config's state
        (`on` / `off` / `unset`; None when not connected)."""
        from syncmusic.devices.Rockbox.rockbox import Rockbox, RockboxSettings

        cfg = await self.config(device_id)
        if cfg.type != Rockbox.type_name:
            raise SyncError(f"{cfg.name} is not a Rockbox player", "invalid")
        setting = RockboxSettings.model_validate(cfg.settings).enable_play_log
        state = None
        if self._connected(cfg) and not self.ctx.jobs.device_busy(device_id):
            device = await self.open_device(device_id)
            assert isinstance(device, Rockbox)
            state = await device.play_log_state()
        return {"setting": setting, "state": state}

    def _connected(self, cfg: DeviceConfig) -> bool:
        if self._usb_mtp(cfg):
            return self.detector.by_device(cfg.id) is not None and cfg.id not in self._ejecting
        if self._network(cfg):
            return True
        if cfg.mount_path:
            return Path(cfg.mount_path).is_dir()
        return self.detector.by_device(cfg.id) is not None and cfg.id not in self._ejecting

    async def set_play_log(self, device_id: str, on: bool) -> dict[str, Any]:
        """Switch the Rockbox play log on or off: the device setting changes, and a connected player's
        config.cfg is written at once - a device action like Rename, not a sync; refused while a job runs on it. A
        player that is not connected gets it at the next write. Returns `play_log()` plus `applied`."""
        from syncmusic.devices.Rockbox.rockbox import Rockbox

        cfg = await self.config(device_id)
        if cfg.type != Rockbox.type_name:
            raise SyncError(f"{cfg.name} is not a Rockbox player", "invalid")
        lock = self.ctx.jobs.device_lock(device_id)
        if lock.locked() or self.ctx.jobs.running_for_device(device_id) is not None:
            raise SyncError(f"{cfg.name} is busy", "busy")
        if self._connected(cfg) and not await self._home_of_mounted(device_id):
            raise SyncError(
                f"the play log of {cfg.name} is set by its home instance: make this instance home first",
                "not_home",
            )
        applied = False
        async with lock:
            cfg = cfg.model_copy(update={"settings": {**cfg.settings, "enable_play_log": on}})
            await self.store.put_device(cfg)
            await self.load()
            self.drop_plans(device_id)
            if self._connected(cfg):
                device = await self.open_device(device_id)
                assert isinstance(device, Rockbox)
                await asyncio.to_thread(device.write_play_log, on)
                applied = True
        self.ctx.bus.publish("device.play_log", device_id=device_id, on=on, applied=applied)
        return {**await self.play_log(device_id), "applied": applied}

    # --- plan / apply ----------------------------------------------------------------------------

    async def _snapshots(self, cfg: DeviceConfig) -> dict[str, LibrarySnapshot]:
        libs = await self.libraries_of(cfg)
        if not libs:
            raise SyncError(f"{cfg.name} has no templates that pull from a library", "invalid")
        return await self.library_snapshots(libs, cfg)

    async def library_snapshots(
        self,
        library_ids: list[str],
        device: DeviceConfig | None = None,
        accounts: dict[str, str] | None = None,
    ) -> dict[str, LibrarySnapshot]:
        """Snapshots of these libraries (as the device's accounts, or as `accounts`: library id → account id); SyncError("library_unavailable") when one cannot be read."""
        out: dict[str, LibrarySnapshot] = {}
        counted = 0  # items of the libraries already read (planning progress)
        n = len(library_ids)
        for i, lid in enumerate(library_ids):
            lib = (
                await self.library_as(lid, accounts.get(lid))
                if accounts is not None
                else await self.library(lid, device)
            )

            def progress(done: int, total: int | None, i: int = i, base: int = counted) -> None:
                part = min(done / total, 1.0) if total else 0.0
                planprogress.step("libraries", fraction=(i + part) / n, items=base + done)

            planprogress.step("libraries", fraction=i / n, items=counted, detail=lib.config.name)
            health = await lib.check()
            if not health.ok:
                raise SyncError(
                    f"library {lid!r} is not available: {health.message}", "library_unavailable"
                )
            if lib.id not in self._source_checked and not lib.account:
                await self.refresh_source_id(lib)
            try:
                with on_progress(progress):
                    out[lid] = await self.snapshots.get(lib)
                counted += len(out[lid].tracks)
            except Exception as e:  # noqa: BLE001 - a failed snapshot is never planned on
                raise SyncError(
                    f"library {lid!r} is not available: {lib.failure_reason(e)}",
                    "library_unavailable",
                ) from e
        return out

    async def plan(
        self,
        device_id: str,
        *,
        force: bool = False,
        auto: bool = False,
        removal: bool = False,
        register: bool = True,
    ) -> PlanContext:
        """The device's sync plan. `removal`: for Player contents / Remove orphans, which only delete - a device
        without libraries is planned too (all its items are orphans then). `register=False`: not kept
        for a review, so a plan the user is looking at stays (update syncs)."""
        cfg = await self.config(device_id)
        if cfg.mount_path:  # configs stored were never checked
            await self.check_mount_path(cfg.mount_path, cfg.id, cfg.type)
        device = await self.open_device(device_id)
        attached = await self.libraries_of(cfg)
        snaps = {} if removal and not attached else await self._snapshots(cfg)
        planprogress.step("device", detail=cfg.name)
        manifest = await self._read_manifest(cfg, device)
        snaps.update(await self._managed_snapshots(cfg, manifest, attached))
        planprogress.step("compare")
        plan = await self._space_plan(
            cfg, device, snaps, manifest, cfg.processors, force=force, auto=auto, attached=attached
        )
        planprogress.step("checks")
        if manifest is not None:
            plan.resuming = manifest.header.interrupted
        merging = [
            a.item.track
            for a in plan.actions
            if a.item is not None and a.kind in (ActionKind.ADD, ActionKind.REPLACE)
        ]
        if merging and (note := await self.preparer.merge_warning(merging)):
            plan.warnings.append(note)
        if manifest is not None and device.effective_capabilities().supports_playlists:
            selected = {p.name for p in plan.playlists}
            if gone := [n for n in manifest.playlist_created if n not in selected]:
                # made on the device, saved to a library, selected by no rule
                plan.warnings.append(
                    f"playlists {', '.join(repr(n) for n in gone)}: in the library, selected by no rule "
                    "- removed from the device"
                )
        if cfg.reverse_sync.enabled:
            plan.warnings += await device.feedback_warnings()
            if manifest is not None and register:
                # what the reverse sync will send: a dry read, nothing consumed
                try:
                    fb = await device.read_feedback(manifest)
                except Exception as e:  # noqa: BLE001 - the listing is extra, the plan stands without it
                    log.warning("plan %s: listening data not read: %s", cfg.id, e)
                else:
                    reverse_changes(plan, fb, cfg.reverse_sync, cfg.sync.rating_priority)
                if cfg.reverse_sync.playlists and cfg.sync.playlist_conflicts == "ask":
                    plan.playlist_conflicts = await self._playlist_conflicts(cfg, device, manifest)
        if device.db_size_limit:  # the iTunesDB limit
            estimated = await device.estimate_db_size(
                items_after(plan), sum(len(p.refs) for p in plan.playlists)
            )
            db = check_db_size(plan, await device.db_size(), estimated, device.db_size_limit)
            if db.level != "ok" and register:  # an update check adds nothing: not news
                self.ctx.bus.publish(
                    "sync.db_size_warning",
                    device_id=cfg.id,
                    plan_id=plan.id,
                    level=db.level,
                    estimated=db.estimated,
                    limit=db.limit,
                )
        foreign = manifest is not None and foreign_manifest(manifest, cfg)
        if foreign:
            assert manifest is not None
            plan.needs_confirmation = True
            plan.confirmation_reasons.insert(
                0,
                f"this device was synced by another registration ({manifest.header.device_id!r}): "
                "confirm to take over its managed files, or cancel and wipe it",
            )
        matches: list[tuple[DeviceItem, Track]] = []
        existing = 0
        if plan.first_sync:
            tracks = [t for s in snaps.values() for t in s.tracks.values()]
            found = await device.read_contents()
            existing = len(found.items)
            matches = match_existing(found, tracks)
        pc = PlanContext(plan, matches, existing, foreign)
        if register:
            self.plans[plan.id] = pc
            self._trim_plans(device_id, plan.id)
        return pc

    async def _playlist_conflicts(
        self, cfg: DeviceConfig, device: Device, manifest: Manifest
    ) -> list[PlaylistConflict]:
        """The playlist conflicts step 0 would leave alone ("ask"): a dry run on a manifest copy."""
        try:
            libraries, _ = await self._libraries_now(cfg)
            report = ReverseReport()
            await push_playlists(
                device, manifest_io.copy(manifest), libraries, report, "ask", cfg.name, dry=True
            )
        except Exception as e:  # noqa: BLE001 - the listing is extra, the plan stands without it
            log.warning("plan %s: device playlists not read: %s", cfg.id, e)
            return []
        managed = {*manifest.playlists, *manifest.playlist_created}
        return [
            PlaylistConflict(name=n, kind="reorder" if n in managed else "name_taken")
            for n in dict.fromkeys(report.playlist_conflicts)
        ]

    async def _space_plan(
        self,
        cfg: DeviceConfig,
        device: Device,
        snaps: dict[str, LibrarySnapshot],
        manifest: Manifest | None,
        processors: list[ProcessorSlot],
        *,
        force: bool = False,
        auto: bool = False,
        attached: list[str] | None = None,
    ) -> SyncPlan:
        missing = await device.missing(manifest) if manifest else set()
        capacity, free = await device.capacity(), await device.free_space()
        templates = await self.store.templates()
        library_names = {lib.id: lib.name for lib in await self.store.libraries()}
        account_names = await self._account_names(cfg)
        # pure CPU, seconds on a big library: in a worker thread, so the API and the event stream stay
        # responsive meanwhile (every input is private to this plan - read_manifest returns a copy)
        return await asyncio.to_thread(
            functools.partial(
                plan_with_space,
                cfg,
                snaps,
                manifest,
                device.effective_capabilities(),
                Chain.from_slots(processors, self.ctx),
                capacity=capacity,
                free=free,
                commit_reserve=device.commit_reserve,
                templates=templates,
                auto=auto,
                force=force,
                missing=missing,
                library_names=library_names,
                account_names=account_names,
                attached=attached,
            )
        )

    async def _managed_snapshots(
        self, cfg: DeviceConfig, manifest: Manifest | None, attached: list[str]
    ) -> dict[str, LibrarySnapshot]:
        """Snapshots of the enabled libraries the device holds managed items of but no longer draws from
        (its template was unlinked): their items are unselected, not orphans. One that cannot be read is
        left out (its items stay orphans)."""
        if manifest is None:
            return {}
        held = {e.ref.library_id for e in manifest.items.values()}
        enabled = [c.id for c in await self.store.libraries() if c.enabled]
        out: dict[str, LibrarySnapshot] = {}
        for lid in enabled:
            if lid in held and lid not in attached:
                try:
                    out.update(await self.library_snapshots([lid], cfg))
                except SyncError as e:
                    log.info("%s: managed items of library %s not checked: %s", cfg.id, lid, e)
        return out

    async def _account_names(self, cfg: DeviceConfig) -> dict[str, str]:
        """Library id → the name of the library account the device signs in as."""
        out: dict[str, str] = {}
        for lid, aid in cfg.library_accounts.items():
            lib = await self.store.library(lid)
            if lib is not None and aid in lib.accounts:
                out[lid] = lib.accounts[aid].name
        return out

    async def fit_estimate(self, device_id: str, to: FitTarget, kbps: int) -> dict[str, Any]:
        """Transcode to fit: what the device's plan needs with lossless files and lossy ones above `kbps`
        converted to `to` - nothing is stored."""
        cfg = await self.config(device_id)
        try:
            processors = with_fit_rule(cfg.processors, to, kbps)
        except ValueError as e:
            raise SyncError(str(e), "invalid") from e
        device = await self.open_device(device_id)
        snaps = await self._snapshots(cfg)
        manifest = await device.read_manifest()
        plan = await self._space_plan(cfg, device, snaps, manifest, processors)
        converted = sum(
            1
            for a in plan.actions
            if a.item is not None and "transcode" in a.item.steps and a.kind is not ActionKind.KEEP
        )
        s = plan.space
        return {
            "to": to,
            "bitrate_kbps": kbps,
            "needed": s.needed,
            "free": s.free,
            "capacity": s.capacity,
            "fits": s.fits and not plan.does_not_fit,
            # bytes of SyncMusic's files on the device after the plan (planned size, else the file kept as is)
            "on_device": sum(
                a.item.file.size if a.item is not None else a.entry.size if a.entry else 0
                for a in plan.actions
                if a.kind is not ActionKind.REMOVE
            ),
            "converted": converted,
        }

    async def fit_apply(self, device_id: str, to: FitTarget, kbps: int) -> DeviceConfig:
        """Accept a transcode-to-fit offer: the rule goes into the device's transcode processor (the next plan uses
        it)."""
        cfg = await self.config(device_id)
        try:
            processors = with_fit_rule(cfg.processors, to, kbps)
        except ValueError as e:
            raise SyncError(str(e), "invalid") from e
        # the device's own chain from now on, as when the chain is edited by hand: a linked template's
        # next change would drop the fit rule
        return await self.update_device(
            cfg.model_copy(update={"processors": processors, "processor_template_id": None})
        )

    async def profile(self, device_id: str) -> dict[str, Any]:
        """The device profile: every field with its value and source, and a overrides.
        Detection needs the device: when it is not connected (or busy setting its clock) the profile shows the
        defaults, the type's generic entry, the settings and the overrides (`connected: false`)."""
        cfg = await self.config(device_id)
        cls = self._device_class(cfg.type)
        clock = self._clock_tasks.get(device_id)
        try:
            if clock is not None and not clock.done():
                raise SyncError(f"{cfg.name} is setting its clock", "busy")
            device = await self.open_device(device_id)
            connected = True
        except SyncError as e:
            if e.code not in ("not_connected", "busy", "unmountable"):
                raise
            device = cls(
                cfg, Path(self.ctx.config.mount_root) / f".{device_id}-not-connected", self.ctx
            )
            connected = False
        prof = await asyncio.to_thread(device.profile, connected)
        fields = prof.model_dump(mode="json")["fields"]
        if cls.music_only:  # not configurable
            for k in ("supports_music", "supports_podcasts", "supports_audiobooks"):
                fields.pop(k, None)
        return {
            "connected": connected,
            "model": prof.model,
            "label": prof.label,
            "known": prof.known,
            "music_only": cls.music_only,
            "fields": fields,
        }

    async def space(self, device_id: str) -> dict[str, Any]:
        """Capacity and free bytes of a connected device (mounts it like a plan does), the used bytes per media kind
        from its manifest (`by_kind`; `others` = items of other installations; the rest of the used space is `other`) and the net bytes its pending plan
        needs (`planned`, None without a plan) - the device page's capacity bar."""
        device = await self.open_device(device_id)
        last = next(
            (pc.plan for pc in reversed(self.plans.values()) if pc.plan.device_id == device_id),
            None,
        )
        pending = last.space if last else None
        planned = pending.needed if pending else None
        capacity, free = await device.capacity(), await device.free_space()
        unknown = capacity >= UNKNOWN_SPACE
        manifest = await self._read_manifest(await self.config(device_id), device)
        by_kind = {"music": 0, "audiobook": 0, "podcast": 0, "video": 0, "others": 0, "other": 0}
        for e in manifest.items.values() if manifest else ():
            # items another installation keeps on this device get a segment of their own
            by_kind["others" if manifest_io.is_external(e.ref.library_id) else e.kind.value] += (
                e.size
            )
        # FTP without quota: what SyncMusic put there instead of a made-up capacity
        used = sum(by_kind.values()) if unknown else max(0, capacity - free)
        # the rest of the used space: sidecars, the device's database, files SyncMusic did not write
        by_kind["other"] = max(0, used - sum(by_kind.values()))
        cfg = await self.config(device_id)
        rules, _, _ = resolve_rules(cfg.rule_set, await self.store.templates())
        db_size = await device.db_size()
        breakdown = usage_breakdown(
            manifest,
            used=used,
            db_size=db_size,
            rule_names={r.id: r.name or r.id for r in rules},
            library_names={lib.id: lib.name for lib in await self.store.libraries()},
        )
        return {
            **breakdown,
            # the safety margin a plan keeps free; 0 when the space is unknown
            "reserve": 0 if unknown else safety_margin(capacity, cfg.sync),
            # what the last plan left off and why
            "left_off": left_off(last) if last else None,
            "capacity": None if unknown else capacity,
            "free": None if unknown else free,
            "unknown": unknown,
            "used": used,
            "by_kind": by_kind,
            "planned": planned,
            # the part of `planned` that is the commit reserve / safety margin
            "planned_reserve": pending.reserve if pending else None,
            # the iTunesDB now and its limit; None for devices without a database limit
            "db": {
                "size": db_size,
                "limit": device.db_size_limit,
                "level": db_level(db_size or 0, device.db_size_limit),
            }
            if device.db_size_limit
            else None,
        }

    async def positions(self, device_id: str) -> list[dict[str, Any]]:
        """Audiobooks in progress on a connected device that keeps bookmarks: the device's position as of
        the last sync, the library's now and who is ahead. [] for devices without bookmarks."""
        device = await self.open_device(device_id)
        if not device.effective_capabilities().supports_positions_read:
            return []
        cfg = await self.config(device_id)
        manifest = await self._read_manifest(cfg, device)
        if manifest is None:
            return []
        tracks: dict[str, Track] = {}
        for lib_id in {e.ref.library_id for e in manifest.items.values()}:
            snap = await self.snapshots.cached(account_key(lib_id, cfg))
            tracks.update(
                {t.ref.key(): t for t in [*snap.tracks.values(), *snap.books.values()]}
                if snap
                else {}
            )
        books = book_positions.books_on_device(manifest, tracks)
        recorded = await self.store.position_scopes(device_id)
        now: dict[str, dict[str, PositionInfo]] = {}
        out = []
        for b in books:
            lib_pos = scope = None
            try:
                lib = await self.library(b.library_id, cfg)
                if isinstance(lib, FeedbackSink) and lib.capabilities.supports_positions:
                    if b.library_id not in now:
                        now[b.library_id] = await lib.scope_positions()
                    scope = await lib.position_scope(b.first)
                    info = now[b.library_id].get(scope)
                    lib_pos = info.position_s if info else 0.0
            except Exception as e:  # noqa: BLE001 - library unreachable: the device's side alone
                log.info("%s: library position of %s unknown: %s", device_id, b.first.key(), e)
            state, diff = book_positions.compare(
                b.position_s, lib_pos, recorded.get(scope) if scope else None
            )
            out.append(
                {
                    "key": b.first.key(),
                    "library_id": b.library_id,
                    "title": b.title,
                    "author": b.author,
                    "position_s": b.position_s,
                    "library_position_s": lib_pos,
                    "duration_s": b.duration_s,
                    "state": state,
                    "diff_s": diff,
                }
            )
        return out

    def drop_plans(self, device_id: str) -> None:
        """Pending plans of a device become outdated (config changed, format, eject, unplug); so do its
        cached Player contents."""
        self._trim_plans(device_id, keep="")
        self._contents_cache.pop(device_id, None)

    def _trim_plans(self, device_id: str, keep: str) -> None:
        for pid in [
            p for p, c in self.plans.items() if c.plan.device_id == device_id and p != keep
        ]:
            del self.plans[pid]

    def get_plan(self, plan_id: str) -> PlanContext:
        pc = self.plans.get(plan_id)
        if pc is None:
            raise SyncError(f"unknown or outdated plan {plan_id!r}", "not_found")
        return pc

    async def apply(
        self,
        plan_id: str,
        *,
        confirm: bool = False,
        first_sync: FirstSync | None = None,
        confirm_db_limit: bool = False,
        playlist_choices: Mapping[str, Policy] | None = None,
    ) -> JobInfo:
        pc = self.get_plan(plan_id)
        plan = pc.plan
        pc.playlist_choices = dict(playlist_choices or {})
        cfg = await self.config(plan.device_id)
        if plan.does_not_fit:
            raise SyncError(
                "the plan does not fit: edit the rules or plan again with force", "does_not_fit"
            )
        if plan.first_sync and first_sync is None:
            raise SyncError("first sync: choose keep, adopt or wipe", "confirmation_required")
        if plan.needs_confirmation and not (confirm or first_sync):
            raise SyncError("; ".join(plan.confirmation_reasons), "confirmation_required")
        if plan.db is not None and plan.db.level == "over" and not confirm_db_limit:
            # its own confirmation, never implied by `confirm`
            raise SyncError(
                f"the device database would exceed its limit ({plan.db.estimated} > {plan.db.limit} bytes): "
                "apply with confirm_db_limit to take the risk",
                "confirmation_required",
            )
        if (
            first_sync == "wipe"
            and cfg.mount_path
            and not await self._removable(await asyncio.to_thread(Path(cfg.mount_path).resolve))
        ):
            raise SyncError(
                f"refusing to wipe {cfg.mount_path}: a plain directory, not a removable disk "
                "(choose keep or adopt)",
                "invalid",
            )
        if plan.first_sync and first_sync in ("wipe", "adopt"):
            await self._require_home(cfg, "wipe or adopt the files on")
        await self._yield_update(cfg.id)  # a user action wins over an update sync
        if self.ctx.jobs.device_busy(cfg.id):
            raise SyncError(f"{cfg.name} is busy", "busy")
        del self.plans[plan_id]
        self._waiting.pop(cfg.id, None)
        if plan.first_sync and first_sync is not None and cfg.first_sync_choice != first_sync:
            cfg.first_sync_choice = first_sync  # persisted with the device
            await self.store.put_device(cfg)

        async def job(ctx: JobContext) -> dict[str, Any]:
            try:
                report = await self._run_apply(cfg, pc, first_sync, ctx)
            except Exception as e:
                # also a failure before anything was written (the device did not open, ...): notified
                self.ctx.bus.publish(
                    "sync.failed",
                    device_id=cfg.id,
                    job_id=ctx.info.id,
                    error=str(e),
                    suggest_gentle=suggests_gentle(cfg, e),
                )
                raise
            await self.store.put_report(report, ctx.info.id)
            self.ctx.bus.publish(
                "sync.finished",
                device_id=cfg.id,
                job_id=ctx.info.id,
                report_id=report.id,
                status=report.status,
                failed=len(report.failures),
            )
            return report.model_dump(mode="json")

        return self.ctx.jobs.submit("sync", f"sync {cfg.name}", job, device_id=cfg.id)

    async def _sync_time(self, cfg: DeviceConfig, device: Device) -> bool:
        """Set the device clock when it supports it and is connected as a disk.
        Called at eject after unmounting: the iPod writes its own file system in reaction, so never while
        the host has it mounted (scsi refuses that). Never fails the eject; a model that rejects the command
        gets the capability turned off (capability override). True when the clock was set."""
        if not device.effective_capabilities().supports_time_sync or device.block_path is None:
            return False
        try:
            await device.set_time(dt.datetime.now(dt.UTC))
        except TimeSyncRejected as e:
            log.warning("%s: time sync not supported by this device (%s); turned off", cfg.id, e)
            cfg = await self.config(cfg.id)
            cfg.capability_overrides["supports_time_sync"] = False
            await self.store.put_device(cfg)
            await self.load()
            self.ctx.bus.publish("device.time_sync_unsupported", device_id=cfg.id)
        except Exception as e:  # noqa: BLE001 - the clock is a convenience
            log.warning("%s: time sync failed: %s", cfg.id, e)
        else:
            log.info("%s: clock set", cfg.id)
            return True
        return False

    def _clock_done(self, device_id: str, task: asyncio.Task[bool]) -> None:
        """The connect clock task ended (set and settled, skipped, failed or cancelled): the UI showed 'Setting the
        clock…' and re-renders on this event."""
        ok = not task.cancelled() and task.exception() is None and task.result()
        self.ctx.bus.publish("device.clock_done", device_id=device_id, ok=ok)

    def _setting_clock(self, device_id: str) -> bool:
        task = self._clock_tasks.get(device_id)
        return task is not None and not task.done()

    def _clock_due(self, device_id: str) -> bool:
        last = self._clock_set.get(device_id)
        return last is None or time.monotonic() - last >= CLOCK_FRESH_S

    async def _set_clock(self, cfg: DeviceConfig, block_path: str) -> bool:
        """Set the clock of an unmounted disk device that has time sync on and was not set recently, then give the
        iPod SETTLE_S to write its Device files before anything mounts it. True when it was set."""
        if cfg.mount_path or self.ctx.guard is None or not self._clock_due(cfg.id):
            return False
        if cfg.id in self._mounts:  # never while mounted (scsi.set_ipod_time refuses it as well)
            return False
        device = registry.device_class(cfg.type)(cfg, self.ctx.config.mount_root / cfg.id, self.ctx)
        device.block_path = block_path
        if not await self._sync_time(cfg, device):
            return False
        self._clock_set[cfg.id] = time.monotonic()
        self.ctx.bus.publish("device.clock_set", device_id=cfg.id, settle_s=scsi.SETTLE_S)
        cd = self.detector.by_device(cfg.id)
        await self._settle(block_path, cd.data_partition if cd is not None else None)
        return True

    async def _settle(self, block_path: str, data_partition: str | None) -> None:
        """The iPod writes its Device files after its clock is set: nothing mounts or powers it off until it is
        done - as soon as its state is stable, at most SETTLE_S."""
        if self.ctx.guard is not None:
            await scsi.wait_settled(self.ctx.guard, block_path, data_partition)

    async def _clock_before_sync(self, cfg: DeviceConfig) -> None:
        """At a sync, before anything is written: unmount, set the clock, settle - the next open mounts again
        . Nothing happens when time sync is off or the clock was set less than CLOCK_FRESH_S ago."""
        cd = self.detector.by_device(cfg.id)
        if cd is None or cfg.mount_path or not self._clock_due(cfg.id):
            return
        if not await self._home_of_mounted(cfg.id):
            return  # player settings are the home instance's
        probe = registry.device_class(cfg.type)(cfg, self.ctx.config.mount_root / cfg.id, self.ctx)
        if not probe.effective_capabilities().supports_time_sync:
            return
        running = self._clock_tasks.get(cfg.id)
        if running is not None and not running.done():
            await asyncio.shield(running)  # the connect clock is still settling: that one counts
            return
        # registered as the device's clock task before the unmount, so open_device from any other request (Player
        # contents, the sidebar's used space) waits for the settle instead of mounting meanwhile
        task = asyncio.create_task(
            self._unmount_and_set_clock(cfg, cd.disk.path), name=f"clock-{cfg.id}"
        )
        task.add_done_callback(functools.partial(self._clock_done, cfg.id))
        self._clock_tasks[cfg.id] = task
        await asyncio.shield(task)

    async def _unmount_and_set_clock(self, cfg: DeviceConfig, block_path: str) -> bool:
        await self._unmount(cfg.id)
        return await self._set_clock(cfg, block_path)

    async def _libraries_now(
        self, cfg: DeviceConfig
    ) -> tuple[dict[str, Library | None], dict[str, str]]:
        """The device's libraries for the reverse sync; one that is down maps to None (its data stays pending),
        with why by its name and the cause."""
        out: dict[str, Library | None] = {}
        why: dict[str, str] = {}
        for lid in await self.libraries_of(cfg):
            lcfg = await self.store.library(lid)
            name = lcfg.name if lcfg is not None else lid
            lib: Library | None = None
            try:
                lib = await self.library(lid, cfg)
                health = await lib.check()
            except Exception as e:  # noqa: BLE001 - an unavailable library must not stop the others
                log.warning("%s: library %s unavailable for reverse sync: %s", cfg.id, lid, e)
                out[lid] = None
                reason = lib.failure_reason(e) if lib is not None else str(e)
                why[lid] = f"{name} is not reachable: {reason or type(e).__name__}"
                continue
            out[lid] = lib if health.ok else None
            if not health.ok:
                why[lid] = f"{name} is not reachable: {health.message or 'its check failed'}"
        return out, why

    async def _patch_snapshot(
        self, kind: str, ref: ItemRef, data: dict[str, Any], key: str
    ) -> None:
        await self.snapshots.patch(kind, ref, data, key)

    async def _run_reverse(
        self,
        cfg: DeviceConfig,
        device: Device,
        playlist_choices: Mapping[str, Policy] | None = None,
    ) -> ReverseReport:
        libraries, why = await self._libraries_now(cfg)
        report = await self.reverse_sync.run(cfg, device, libraries, why, playlist_choices)
        self.ctx.bus.publish("reverse.finished", device_id=cfg.id, **report.model_dump())
        if (
            report.plays_submitted
            or report.ratings_pushed
            or report.positions_pushed
            or report.playlists_pushed
            or report.playlists_created  # the next plan sees the saved / created playlists
        ):
            # play counts / ratings / progress changed there. Only then: expiring after every reverse sync made the
            # snapshot loop reload it, mark every device dirty and start the next update a minute later
            for lid, lib in libraries.items():
                await self.snapshots.expire(lib.cache_key if lib is not None else lid)
            # e.g. "Most played" on another connected device changes
            await self.mark_libraries_changed(list(libraries), except_device=cfg.id)
        return report

    async def _step0(
        self,
        cfg: DeviceConfig,
        device: Device,
        playlist_choices: Mapping[str, Policy] | None = None,
    ) -> ReverseReport:
        """Reverse sync as step 0 of a sync: a failure is reported, the forward sync goes on."""
        try:
            return await self._run_reverse(cfg, device, playlist_choices)
        except Exception as e:  # noqa: BLE001
            log.exception("%s: reverse sync failed", cfg.id)
            return ReverseReport(errors=[f"reverse sync failed: {e}"], failed=True)

    async def update_sync(self, device_id: str, *, only_if_changed: bool = False) -> JobInfo:
        """An update sync as a device job: reverse sync, then what changed for the items already on
        the device (metadata, ratings, positions, playlists) - no new files, no removals unless the device allows
        them, no review. Refused while another job holds the device; skipped (a report that says why) before the
        first full sync or on a device another registration synced."""
        cfg = await self.config(device_id)
        self._refuse_damaged(cfg)
        if self.ctx.jobs.device_busy(cfg.id):
            raise SyncError(f"{cfg.name} is busy", "busy")
        await self.open_device(cfg.id)  # not connected / unmountable: refused now, not in the job

        async def job(ctx: JobContext) -> dict[str, Any]:
            self.ctx.bus.publish("update.started", device_id=cfg.id, job_id=ctx.info.id)
            started = dt.datetime.now(dt.UTC)
            try:
                # a device without templates has nothing to update from
                if not await self.libraries_of(await self.config(cfg.id)):
                    return {
                        "kind": "update",
                        "status": "skipped",
                        "message": "no templates pull from a library",
                    }
                # the listening data goes to the libraries first and patches their snapshots, so the plan sees an
                # episode played on this device as played
                await self._clock_before_sync(cfg)
                device = await self.open_device(cfg.id)
                manifest = await self._read_manifest(cfg, device)
                reverse = ReverseReport()
                if manifest is not None and not foreign_manifest(
                    manifest, cfg
                ):  # else skipped below
                    ctx.progress(None, None, phase="reverse")
                    reverse = await self._step0(cfg, device)
                # not `auto`: the full sync's confirmations (delete threshold, database limit) are about removing
                # and adding files; an update adds none and holds its removals back itself
                pc = await self.plan(cfg.id, register=False)  # never offered for review
                skip = (
                    "no update before the first full sync"
                    if pc.plan.first_sync
                    else pc.plan.confirmation_reasons[0]
                    if pc.foreign
                    else ""
                )
                if skip:
                    report = SyncReport(
                        id=uuid.uuid4().hex,
                        device_id=cfg.id,
                        plan_id=pc.plan.id,
                        status="skipped",
                        message=f"update sync skipped: {skip}",
                        kind="update",
                        finished_at=dt.datetime.now(dt.UTC),
                    )
                else:
                    removals, held = cfg.update_removals, ""
                    consumed = (
                        set(reverse.consumed)
                        if cfg.update_remove_played and not removals
                        else set()
                    )
                    # deleted from their library: removed by updates too
                    deleted = {a.ref.key() for a in pc.plan.actions if a.deleted}
                    consumed |= deleted
                    n = pc.plan.count(ActionKind.REMOVE)
                    if not removals:
                        n = sum(
                            1
                            for a in pc.plan.actions
                            if a.kind is ActionKind.REMOVE and a.ref.key() in consumed
                        )
                    on_device = sum(1 for a in pc.plan.actions if a.kind is not ActionKind.ADD)
                    if n and over_delete_threshold(n, on_device, cfg.delete_threshold_pct):
                        removals, consumed = False, set()
                        held = (
                            f"{n} of {on_device} items to remove (more than {cfg.delete_threshold_pct} %) "
                            "are left for a full sync to review"
                        )
                    plan = update_only(pc.plan, removals=removals, consumed=consumed)
                    played = sum(
                        1
                        for a in plan.actions
                        if a.kind is ActionKind.REMOVE and a.ref.key() in reverse.consumed
                    )
                    sig = update_signature(plan)
                    if (
                        only_if_changed
                        and not reverse.consumed
                        and not reverse.positions_pushed
                        and not update_writes_items(plan)
                        and self._update_sig.get(cfg.id) == sig
                    ):
                        return {"kind": "update", "status": "skipped", "message": "nothing changed"}
                    # files the manifest lists but the device lost (deleted by an integrity fix, by hand): the
                    # full sync writes them again, the update's read-back does not count them
                    waiting = {a.ref.key() for a in pc.plan.actions if a.kind is ActionKind.ADD}
                    report = await self._run_apply(
                        cfg,
                        PlanContext(plan, []),
                        None,
                        ctx,
                        update=True,
                        waiting=waiting,
                        reverse=reverse,
                    )
                    if held:
                        report.warnings.append(held)
                    if played:
                        report.message = removed_played(played, report.message)
                    gone = sum(1 for a in plan.actions if a.kind is ActionKind.REMOVE and a.deleted)
                    if gone:
                        line = f"removed {gone} deleted from the library"
                        report.message = f"{report.message}; {line}" if report.message else line
                    if report.status == "succeeded":
                        self._update_sig[cfg.id] = sig
                        ctx.committed = (
                            True  # a user action yielding it now does not undo the write (563)
                        )
            except JobCancelled:
                raise  # a full sync or another user action took over: not a failure
            except Exception as e:
                last = await self.store.last_report(cfg.id, "update")
                if (
                    last is None or last.started_at < started
                ):  # failed before the applier stored one
                    failed = SyncReport(
                        id=uuid.uuid4().hex,
                        device_id=cfg.id,
                        status="failed",
                        message=str(e),
                        kind="update",
                        started_at=started,
                        finished_at=dt.datetime.now(dt.UTC),
                    )
                    await self.store.put_report(failed, ctx.info.id)  # the update line says why
                self.ctx.bus.publish(
                    "update.failed", device_id=cfg.id, job_id=ctx.info.id, error=str(e)
                )
                raise
            await self.store.put_report(report, ctx.info.id)
            self.ctx.bus.publish(
                "update.finished",
                device_id=cfg.id,
                job_id=ctx.info.id,
                report_id=report.id,
                status=report.status,
                failed=len(report.failures),
            )
            # what went back to the libraries, for a notification that links to it
            sent = next((a.counts for a in report_activity(report) if a.kind == "sent"), None)
            if sent and report.status != "failed":
                self.ctx.bus.publish(
                    "update.sent", device_id=cfg.id, job_id=ctx.info.id, report_id=report.id, **sent
                )
            # e.g. its read-back failed: nothing raised, still a failed update
            if report.status == "failed":
                self.ctx.bus.publish(
                    "update.failed", device_id=cfg.id, job_id=ctx.info.id, error=report.message
                )
            return report.model_dump(mode="json")

        return self.ctx.jobs.submit(
            "update", f"update {cfg.name}", self._leased(cfg, job), device_id=cfg.id
        )

    @contextlib.asynccontextmanager
    async def _lease(self, cfg: DeviceConfig, ctx: JobContext) -> AsyncIterator[None]:
        """Hold the device lease while a job writes to the device (opened and, for an iPod, clock already set:
        the clock unmounts it). Another installation's fresh lease refuses with `blocked_by_lease`."""
        device = await self.open_device(cfg.id)
        me = manifest_io.context()
        try:
            async with device_lease.held(
                device, me.instance_id, me.instance_name, ctx.info.id, self._mount_check(cfg)
            ):
                yield
        except device_lease.LeaseHeld as e:
            raise SyncError(f"{cfg.name} is {e}", "blocked_by_lease") from e

    def _leased(
        self, cfg: DeviceConfig, run: Callable[[JobContext], Awaitable[dict[str, Any]]]
    ) -> Callable[[JobContext], Awaitable[dict[str, Any]]]:
        """The job `run` under the device lease (clock first, see `_lease`)."""

        async def job(ctx: JobContext) -> dict[str, Any]:
            await self._clock_before_sync(cfg)
            async with self._lease(cfg, ctx):
                return await run(ctx)

        return job

    async def _run_apply(
        self,
        cfg: DeviceConfig,
        pc: PlanContext,
        first_sync: FirstSync | None,
        ctx: JobContext,
        update: bool = False,
        waiting: set[str] | None = None,
        reverse: ReverseReport | None = None,
    ) -> SyncReport:
        """`reverse`: step 0 already ran (an update sync plans after it, under its own lease)."""
        if reverse is not None:
            return await self._apply_steps(cfg, pc, first_sync, ctx, update, waiting, reverse)
        await self._clock_before_sync(cfg)
        async with self._lease(cfg, ctx):
            return await self._apply_steps(cfg, pc, first_sync, ctx, update, waiting, None)

    async def _apply_steps(
        self,
        cfg: DeviceConfig,
        pc: PlanContext,
        first_sync: FirstSync | None,
        ctx: JobContext,
        update: bool,
        waiting: set[str] | None,
        reverse: ReverseReport | None,
    ) -> SyncReport:
        if reverse is None:
            await self._clock_before_sync(cfg)
        device = await self.open_device(cfg.id)
        libraries = {lid: await self.library(lid, cfg) for lid in await self.libraries_of(cfg)}
        if reverse is None:
            ctx.progress(None, None, phase="reverse")  # the job view's phases
            reverse = await self._step0(
                cfg, device, pc.playlist_choices
            )  # before anything on the device changes
        chain = Chain.from_slots(cfg.processors, self.ctx)
        writes_ratings = device.effective_capabilities().supports_ratings_write
        plan = ratings_after_reverse(pc.plan, reverse, cfg, writes_ratings, libraries)
        plan = defer_device_rewrites(plan, reverse, writes_ratings)
        plan = playlists_after_reverse(plan, reverse)  # the lists step 0 saved to the library
        base: Manifest | None = None
        if first_sync == "wipe":
            await device.wipe()
        elif first_sync == "adopt" and pc.matches:
            plan, base = await self._adopt(cfg, device, chain, pc)
        if not update:
            self.ctx.bus.publish("sync.started", device_id=cfg.id, plan_id=plan.id)
        applier = Applier(
            device,
            libraries,
            chain,
            self.preparer,
            check_cancelled=ctx.check_cancelled,
            progress=ctx.progress,
            check_device=self._mount_check(cfg),
            device_positions=None if reverse.failed else reverse.device_positions,
            workers=self.ctx.jobs.prepare_workers,
            pacer=None
            if update or device.network
            else gentle_pacer(
                cfg.sync, device.mount, (await self.store.app_settings()).write_limit_mb_s
            ),
            no_cache=NoCache.of(cfg.sync),
            reverse_ok=not reverse.failed,
        )
        try:
            report = await applier.apply(plan, base)
            report.reverse = reverse
            report.first_sync = first_sync if pc.plan.first_sync else None
            report.kind = "update" if update else "full"
            if report.status in ("succeeded", "partial"):
                ctx.progress(None, None, phase="verify")  # keeps the outcome message
                await self._verify_written(cfg, device, report, plan, waiting or pc.waiting)
                if not update:
                    await self._write_device_templates(cfg, device)
            if not update and not device.network:
                await self._record_health(cfg, report=report, job_id=ctx.info.id)
            self._plan_auto_eject(cfg, device, report, ctx.info.id, update)
            return report
        except Exception as e:
            if not update and storage_failure(e):
                await self._record_health(cfg, error=str(e), job_id=ctx.info.id)
            report = SyncReport(
                id=uuid.uuid4().hex,
                device_id=cfg.id,
                plan_id=plan.id,
                status="failed",
                message=str(e),
                reverse=reverse,
                kind="update" if update else "full",
                suggest_gentle=not update and suggests_gentle(cfg, e),
            )
            await self.store.put_report(report, ctx.info.id)
            raise

    async def _write_device_templates(self, cfg: DeviceConfig, device: Device) -> None:
        """After a full sync: the device's templates file gets this installation's linked templates (others' rows
        stay). A failure costs only the import offer on another installation: logged, never fails the sync."""
        mctx = manifest_io.context()
        if not mctx.instance_id:
            return
        try:
            ours = []
            for tid in cfg.rule_set.template_ids:
                tpl = await self.store.template(tid)
                if tpl is not None:
                    ours.append(
                        device_templates.export(
                            tpl, mctx.libraries, mctx.instance_id, mctx.instance_name
                        )
                    )
            old_raw = await device.read_state(device_templates.FILE_NAME)
            rows = device_templates.merge(device_templates.loads(old_raw), ours, mctx.instance_id)
            raw = device_templates.dumps(rows)
            if raw != old_raw:
                await device.write_state(device_templates.FILE_NAME, raw)
        except Exception as e:  # noqa: BLE001 - optional extra, see the docstring
            log.warning("%s: could not write the templates file: %s", cfg.name, e)

    async def device_templates(self, device_id: str) -> list[dict[str, Any]]:
        """The templates other installations stored on the device and what this one can do with each:
        `linked` (ours or a copy linked to the device), `copy` (a local copy exists; `updated` when the device has a
        newer version), `importable`, or `unavailable` (draws only from libraries we do not have)."""
        cfg = await self.config(device_id)
        device = await self.open_device(cfg.id)
        rows = device_templates.loads(await device.read_state(device_templates.FILE_NAME))
        mctx = manifest_io.context()
        local = await self.store.templates()
        copies = {t.origin_uid: t for t in local.values() if t.origin_uid}
        out = []
        for row in rows:
            if row.instance_id == mctx.instance_id:
                continue
            copy = copies.get(row.template_uid)
            if copy is not None:
                state = "copy"
            elif device_templates.importable(row, mctx):
                state = "importable"
            else:
                state = "unavailable"
            out.append(
                {
                    "uid": row.template_uid,
                    "name": row.name,
                    "instance_name": row.instance_name,
                    "updated_at": row.updated_at.isoformat(),
                    "state": state,
                    "copy_id": copy.id if copy else None,
                    "linked": bool(copy and copy.id in cfg.rule_set.template_ids),
                    "updated": bool(
                        copy
                        and copy.origin_updated_at is not None
                        and row.updated_at > copy.origin_updated_at
                    ),
                    "missing_libraries": device_templates.unmatched(row, mctx),
                }
            )
        return out

    async def import_device_templates(self, device_id: str, uids: list[str]) -> list[str]:
        """Copy the device's templates `uids` (another installation's) into this installation and link them to
        the device; an unedited library default links the local library's own default. Never overwrites a local
        template. Returns the linked template ids."""
        cfg = await self.config(device_id)
        device = await self.open_device(cfg.id)
        rows = {
            r.template_uid: r
            for r in device_templates.loads(await device.read_state(device_templates.FILE_NAME))
        }
        mctx = manifest_io.context()
        linked: list[str] = []
        for uid in uids:
            row = rows.get(uid)
            if row is None:
                raise SyncError(f"{cfg.name} has no template {uid}", "not_found")
            if not device_templates.importable(row, mctx):
                raise SyncError(f"{row.name}: none of its libraries is available here", "invalid")
            local = await self.store.templates()
            copy = next((t for t in local.values() if t.origin_uid == uid), None)
            if copy is None and row.default_key:
                for lid in device_templates.local_libraries(row, mctx).values():
                    own = await self.store.template(rule_defaults.default_id(lid, row.default_key))
                    if own is not None:
                        copy = own
                        break
            if copy is None:
                copy = device_templates.to_local(row, mctx, _free_id(row.name, local))
                await self.store.put_template(copy)
            linked.append(copy.id)
        ids = list(dict.fromkeys([*cfg.rule_set.template_ids, *linked]))
        await self.update_device(
            cfg.model_copy(
                update={"rule_set": cfg.rule_set.model_copy(update={"template_ids": ids})}
            )
        )
        return linked

    async def update_template_copy(self, device_id: str, template_id: str) -> RuleTemplate:
        """Replace the local copy `template_id` with the device's newer version of its original."""
        cfg = await self.config(device_id)
        tpl = await self.store.template(template_id)
        if tpl is None or not tpl.origin_uid:
            raise SyncError(
                f"{template_id} is not a copy of another installation's template", "invalid"
            )
        device = await self.open_device(cfg.id)
        rows = device_templates.loads(await device.read_state(device_templates.FILE_NAME))
        row = next((r for r in rows if r.template_uid == tpl.origin_uid), None)
        if row is None:
            raise SyncError(f"{cfg.name} has no newer version of {tpl.name}", "not_found")
        fresh = device_templates.to_local(row, manifest_io.context(), tpl.id)
        await self.store.put_template(fresh)
        return fresh

    def _plan_auto_eject(
        self, cfg: DeviceConfig, device: Device, report: SyncReport, job_id: str, update: bool
    ) -> None:
        """Eject after a sync: only a succeeded one, never a server or a folder; the eject runs once
        the job has let go of the device. A sync that did not succeed says why the device stayed mounted."""
        wanted = cfg.sync.eject_after == "any" or (cfg.sync.eject_after == "full" and not update)
        if not wanted or (device.network and not device.usb_mtp) or cfg.mount_path:
            return
        if report.status != "succeeded":
            report.warnings.append(
                f"{cfg.name} was not ejected: the sync did not finish without problems"
            )
            return
        report.auto_eject = True
        task = asyncio.create_task(self._auto_eject(cfg.id, job_id), name=f"auto-eject-{cfg.id}")
        self._update_tasks.add(task)
        task.add_done_callback(self._update_tasks.discard)

    async def _auto_eject(self, device_id: str, job_id: str) -> None:
        info = await self.ctx.jobs.wait(job_id)  # its task ended: the device lock is free
        if info.status is not JobStatus.SUCCEEDED:
            return
        if self.ctx.jobs.running_for_device(device_id) is not None:
            log.info("%s: not ejected after the sync: another job is queued", device_id)
            return
        try:
            await self.eject(device_id)
            log.info("%s: ejected after the sync: it can be unplugged", device_id)
            cfg = await self.config(device_id)
            self.ctx.bus.publish(
                "device.auto_ejected", device_id=device_id, name=cfg.name, type=cfg.type
            )
        except SyncError as e:
            log.warning("%s: eject after the sync failed: %s", device_id, e)
            self.ctx.bus.publish("device.eject_failed", device_id=device_id, error=str(e))

    async def _verify_written(
        self,
        cfg: DeviceConfig,
        device: Device,
        report: SyncReport,
        plan: SyncPlan | None = None,
        waiting: set[str] | None = None,
    ) -> None:
        """Read back what the device will see: flush, unmount and mount again a file
        system we mounted (the reads then come from the disk, not the page cache), then every manifest entry
        must be on the device (iPod: in the iTunesDB re-read by pygpod, file present). A failed check turns
        the report `failed` instead of telling the user the device holds music it does not. `cfg.sync.verify`:
        `sample` checks a stable 10 % of the items, `off` skips the read-back. `waiting`: manifest keys
        an update sync knew to be missing and leaves to the next full sync - not a failure, a warning."""
        if cfg.sync.verify == "off":
            return
        m = self._mounts.get(cfg.id)
        shared = m is not None and any(
            o.path == m.path for d, o in self._mounts.items() if d != cfg.id
        )
        if m is not None and m.owned and not shared:
            await self._unmount(cfg.id)
            device = await self.open_device(cfg.id)
        else:  # a directory or a mount someone else owns: flush, then read back what we can
            await asyncio.to_thread(mnt.syncfs, device.mount)
        manifest = await self._read_manifest(cfg, device)
        if manifest is None:
            missing, total = {"(manifest)"}, 0
        else:
            if cfg.sync.verify == "sample":
                manifest = manifest.model_copy(update={"items": verify_sample(manifest.items)})
            missing, total = await device.missing(manifest), len(manifest.items)
            known = missing & (waiting or set())
            if known:
                missing -= known
                total -= len(known)
                one = len(known) == 1
                report.warnings.append(
                    f"{len(known)} {'item is' if one else 'items are'} missing on the device: the "
                    f"next full sync writes {'it' if one else 'them'} again"
                )
        report.counts["verified"] = total - len(missing) if manifest is not None else 0
        if missing:
            report.status = "failed"
            what = (
                "its manifest is unreadable"
                if manifest is None
                else f"{len(missing)} of {total} items are not on it"
            )
            report.message = f"verification failed: after flushing the device, {what}"
            # which tracks: the plan knows them by name, the manifest where they should be
            actions = {a.ref.key(): a for a in plan.actions} if plan is not None else {}
            examples = []
            for key in sorted(missing):
                a = actions.get(key)
                entry = manifest.items.get(key) if manifest is not None else None
                path = entry.path if entry is not None else ""
                # not in the plan (a removal job): the file's name, never only the id
                name = PurePosixPath(path).stem if path else key
                item = describe(a) if a is not None else name
                report.failures.append(
                    ItemFailure(
                        ref=key,
                        title=a.title if a is not None else name,
                        error="not on the device after flushing",
                        item=item,
                        step="verify",
                        path=path,
                    )
                )
                examples.append(f"{item} ({path or key})")
            log.error("%s: %s (e.g. %s)", cfg.id, report.message, "; ".join(examples[:5]))

    async def _adopt(
        self, cfg: DeviceConfig, device: Device, chain: Chain, pc: PlanContext
    ) -> tuple[SyncPlan, Manifest]:
        """Manifest entries for matched files, then a fresh plan on top of it (adopted items become keeps)."""
        caps = device.effective_capabilities()
        manifest = Manifest(header=ManifestHeader(device_id=cfg.id))
        for item, t in pc.matches:
            manifest.items[t.ref.key()] = ManifestEntry(
                ref=t.ref,
                device_ref=item.device_ref,
                path=item.path,
                size=item.size,
                source_state=t.source_state,
                chain_hash=chain.chain_hash(initial_plan_item(t), caps),
                processing=chain.processing(initial_plan_item(t), caps),
                caps_v=CAPS_HASH_VERSION,
                meta_hash=meta_hash(t),
                meta=meta_snapshot(t),
                kind=t.kind,
                duration_s=t.duration_s,
                last_playcount=item.stats.playcount if item.stats else 0,
                last_rating=item.stats.rating if item.stats else None,
            )
        snaps = await self._snapshots(cfg)
        plan = plan_with_space(
            cfg,
            snaps,
            manifest,
            caps,
            chain,
            capacity=await device.capacity(),
            free=await device.free_space(),
            commit_reserve=device.commit_reserve,
            templates=await self.store.templates(),
            force=bool(pc.plan.space.dropped),
        )
        return plan, manifest

    # --- player contents ----------------------------------------------------------------

    async def _readable_snapshots(
        self, cfg: DeviceConfig, manifest: Manifest | None = None
    ) -> dict[str, LibrarySnapshot]:
        """Snapshots of the attached libraries, and of the enabled ones the manifest holds items of, that
        can be read now (the others are left out) - the same libraries a plan reads."""
        attached = await self.libraries_of(cfg)
        out: dict[str, LibrarySnapshot] = {}
        for lid in attached:
            try:
                out.update(await self.library_snapshots([lid], cfg))
            except SyncError as e:
                log.info("%s: contents without library %s: %s", cfg.id, lid, e)
        out.update(await self._managed_snapshots(cfg, manifest, attached))
        return out

    async def _enabled_libraries(self) -> list[str]:
        return [c.id for c in await self.store.libraries() if c.enabled]

    async def feedback(self, device_id: str) -> dict[str, Any]:
        """Listening data of the device that has not reached its library: `pending` (oldest first:
        kind, the item's title from the library's stored snapshot, library, value, attempts, last error, when it
        was captured and last tried) and `dropped` (newest first, given up after the attempt limit)."""
        cfg = await self.config(device_id)
        names = {c.id: c.name for c in await self.store.libraries()}
        snaps: dict[str, LibrarySnapshot | None] = {}

        async def row(kind: str, data: dict[str, Any]) -> dict[str, Any]:
            ref = (
                PlayEvent.model_validate(data).ref
                if kind == "play"
                else ItemRef.parse(str(data["ref"]))
            )
            if ref.library_id not in snaps:
                snaps[ref.library_id] = await self.snapshots.cached(
                    account_key(ref.library_id, cfg)
                )
            snap = snaps[ref.library_id]
            track = snap.track(ref.item_id) if snap else None
            value = (
                data.get("played_at")
                if kind == "play"
                else data.get("rating")
                if kind == "rating"
                else data.get("position_s")
            )
            return {
                "kind": kind,
                "key": ref.key(),
                "title": track.title if track else ref.item_id,
                "artist": (track.artist or track.author) if track else None,
                "library_id": ref.library_id,
                "library": names.get(ref.library_id, ref.library_id),
                "value": value,
            }

        pending = [
            {
                **await row(r.kind, r.data),
                "attempts": r.attempts,
                "tries": r.tries,  # every failed delivery; `attempts` = refusals
                "error": r.last_error,
                "captured_at": r.created_at,
                "tried_at": r.tried_at,
            }
            for r in await self.store.pending_feedback(cfg.id)
        ]
        dropped = [
            {
                **await row(d["kind"], d["data"]),
                "attempts": d["attempts"],
                "error": d["error"],
                "dropped_at": d["dropped_at"],
            }
            for d in await self.store.dropped_feedback(cfg.id)
        ]
        return {"pending": pending, "dropped": dropped}

    async def clear_dropped_feedback(self, device_id: str) -> None:
        await self.store.clear_dropped_feedback((await self.config(device_id)).id)

    async def contents(self, device_id: str, refresh: bool = False) -> list[dict[str, Any]]:
        """Everything on the device: what SyncMusic put there and what other tools did (`foreign`),
        with the device's own names and tags; orphans marked (core/contents.py). One read per device at a
        time: it runs on its own and survives the request that started it (a reload re-attaches)."""
        if refresh:
            self._contents_cache.pop(device_id, None)
        cached = self._contents_cache.get(device_id)
        stamp = await self._contents_stamp(device_id)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        if cached is not None or stamp is None:
            # why the cache missed: a stamp that keeps changing re-reads and re-warms every time
            log.debug(
                "contents of %s not cached: stamp %s -> %s", device_id, cached and cached[0], stamp
            )
        task = self._contents_reads.get(device_id)
        if task is None or task.done():
            task = asyncio.create_task(self._read_contents(device_id), name=f"contents-{device_id}")
            # a read whose every request went away must not log "exception never retrieved"
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            self._contents_reads[device_id] = task
        return await asyncio.shield(task)

    async def contents_etag(self, device_id: str) -> str | None:
        """An ETag of the cached Player contents while they are still current: the browser asks
        again with it and gets a 304 instead of every row once more. None: nothing cached or it is stale."""
        cached = self._contents_cache.get(device_id)
        if cached is None or cached[0] != await self._contents_stamp(device_id):
            return None
        return '"' + hashlib.sha1(cached[0].encode()).hexdigest()[:20] + '"'

    async def _contents_stamp(self, device_id: str) -> str | None:
        """What changes when the device's contents do: the mount, the mtimes of the manifest
        and the iPod database, and the enabled libraries. None (no cache): not mounted locally (a network device) or nothing to look at."""
        m = self._mounts.get(device_id)
        root = m.path if m is not None else None
        if root is None:
            cfg = await self.config(device_id)
            root = Path(cfg.mount_path) if cfg.mount_path else None
        if root is None:
            return None
        # orphans depend on which libraries exist too (one removed or disabled orphans its items)
        libs = ",".join(sorted(await self._enabled_libraries()))

        def stat() -> str | None:
            parts = []
            for rel in (manifest_io.MANIFEST_PATH, Path("iPod_Control/iTunes/iTunesDB")):
                try:
                    st = (root / rel).stat()
                except OSError:
                    continue
                parts.append(f"{rel}:{st.st_mtime_ns}:{st.st_size}")
            return f"{root}|{'|'.join(parts)}|{libs}" if parts else None

        return await asyncio.to_thread(stat)

    async def _read_manifest(self, cfg: DeviceConfig, device: Device) -> Manifest | None:
        """The device's manifest; its item keys are recorded as what the device holds (the library browser's "On devices" chips), written only when they changed. A foreign manifest is not."""
        manifest = await device.read_manifest()
        if manifest is not None and not foreign_manifest(manifest, cfg):
            keys = frozenset(manifest.items)
            if self._device_items.get(cfg.id) != keys:
                await self.store.set_device_items(cfg.id, set(keys))
                self._device_items[cfg.id] = keys
        return manifest

    async def device_items(self) -> dict[str, set[str]]:
        """Per registered device the item keys it held when its manifest was last read."""
        return await self.store.device_items()

    async def _read_contents(self, device_id: str) -> list[dict[str, Any]]:
        cfg = await self.config(device_id)
        device = await self.open_device(device_id)
        manifest = await self._read_manifest(cfg, device)
        found = await device.read_contents()
        snaps = await self._readable_snapshots(cfg, manifest) if manifest is not None else {}
        stamp = await self._contents_stamp(device_id)
        rows = player_contents.contents(found, manifest, snaps, await self._enabled_libraries())
        if stamp is not None:
            paths = {r["key"]: r["path"] for r in rows if not r["missing"]}
            self._contents_cache[device_id] = (stamp, rows, paths)
        return rows

    async def orphans(self, device_id: str) -> list[dict[str, Any]]:
        """The orphans of a device from its manifest and the library snapshots alone: no walk over
        the device's files, so it answers at once even on a large card (same rule as `contents`)."""
        cfg = await self.config(device_id)
        device = await self.open_device(device_id)
        manifest = await self._read_manifest(cfg, device)
        if manifest is None:
            return []
        snaps = await self._readable_snapshots(cfg, manifest)
        return player_contents.orphans(manifest, snaps, await self._enabled_libraries())

    async def content_file(self, device_id: str, key: str) -> Path:
        """The on-device file of a Player contents row (a manifest key, or `device:<device_ref>` for an item another tool put there) for playback; never a path outside the device."""
        cfg = await self.config(device_id)
        device = await self.open_device(device_id)
        rel: str | None = None
        cached = self._contents_cache.get(device_id)
        if cached is not None and cached[0] == await self._contents_stamp(device_id):
            # a cover or a playback of a listed row: no database or manifest read per request
            rel = cached[2].get(key)
        if rel is None and key.startswith(player_contents.FOREIGN_PREFIX):
            ref = key.removeprefix(player_contents.FOREIGN_PREFIX)
            found = await device.read_contents()
            rel = next((i.path for i in found.items if i.device_ref == ref), None)
        elif rel is None:
            rel = (await self._manifest_paths_of(device_id, device)).get(key)
        if rel is None:
            raise SyncError(f"{key!r} is not on {cfg.name}", "not_found")
        root = await asyncio.to_thread(device.mount.resolve)
        path = await asyncio.to_thread((root / rel).resolve)
        if not path.is_relative_to(root) or not await asyncio.to_thread(path.is_file):
            raise SyncError(f"the file of {key!r} is missing on {cfg.name}", "not_found")
        return path

    async def _manifest_paths_of(self, device_id: str, device: Device) -> dict[str, str]:
        """key -> path of the device's manifest, parsed again only when its size or mtime changed."""

        def stamp() -> tuple[int, int] | None:
            try:
                st = (device.mount / manifest_io.MANIFEST_PATH).stat()
            except OSError:
                return None
            return (st.st_size, st.st_mtime_ns)

        st = await asyncio.to_thread(stamp)
        cached = self._manifest_paths.get(device_id)
        if st is not None and cached is not None and cached[0] == st:
            return cached[1]
        manifest = await device.read_manifest()
        paths = {k: e.path for k, e in manifest.items.items()} if manifest else {}
        if st is not None:
            self._manifest_paths[device_id] = (st, paths)
        return paths

    async def remove_from_device(
        self, device_id: str, keys: list[str], exclude: bool = True
    ) -> JobInfo:
        """Delete these items from the device as a sync job; with `exclude` (default) the rules change first so the
        next sync does not bring them back (core/contents.py) - without it no rule changes and the next sync puts
        what the templates select back. If planning or starting the job fails the rule change is
        undone."""
        cfg = await self.config(device_id)
        if not keys:
            raise SyncError("nothing selected", "invalid")
        await self._yield_update(cfg.id)  # a user action wins over an update sync
        if self.ctx.jobs.device_busy(cfg.id):
            raise SyncError(f"{cfg.name} is busy", "busy")
        device = await self.open_device(device_id)
        manifest = await device.read_manifest()
        # items other tools put there: removed by the same job, no rule change needed
        foreign = [k for k in keys if k.startswith(player_contents.FOREIGN_PREFIX)]
        extra, unknown = player_contents.foreign_removals(
            await device.read_contents() if foreign else DeviceContents(), foreign
        )
        unknown += [
            k for k in keys if k not in foreign and (manifest is None or k not in manifest.items)
        ]
        if unknown:
            raise SyncError(f"not on {cfg.name}: {', '.join(unknown[:5])}", "invalid")
        if foreign and not is_home(manifest):
            raise SyncError(
                f"only the home instance {home_name(manifest)} can remove files SyncMusic did not put "
                f"on {cfg.name}",
                "not_home",
            )
        others = [k for k in keys if manifest is not None and manifest_io.is_external(k)]
        if others:
            raise SyncError(
                f"{len(others)} of these items belong to another SyncMusic instance's libraries",
                "invalid",
            )
        if manifest is None:
            return self._remove_unmanaged(cfg, extra)
        gone = set(keys) - set(foreign)
        snaps = await self._readable_snapshots(cfg)
        tracks = {
            k: t
            for k, e in manifest.items.items()
            if e.ref.library_id in snaps
            and (t := snaps[e.ref.library_id].track(e.ref.item_id)) is not None
        }
        # the picks lose them and, if a template still selects them, the device's own template excludes them
        tid = own_template_id(cfg.id)
        templates = await self.store.templates()
        own_before = templates.get(tid)
        rs = as_rules(own_before)
        if exclude:
            rs = player_contents.without_manual(rs, gone, tracks, manifest.items)
            draft = {**templates, tid: own_template(cfg, own_before, rs)}
            linked = cfg.rule_set.model_copy(
                update={"template_ids": list(dict.fromkeys([*cfg.rule_set.template_ids, tid]))}
            )
            still = evaluate_ruleset(linked, snaps, draft).items
            rs = add_device_exclusions(rs, [k.split("/", 1)[1] for k in gone if k in still])
        before = cfg
        changed = rs != as_rules(own_before)
        if changed:
            cfg = await self.set_own_rules(cfg, rs)
        try:
            pc = await self.plan(device_id, removal=True)
            plan = player_contents.only_removals(pc.plan, gone)
            plan.actions += extra
            # items already missing on the device are left to the next full sync
            missing = {a.ref.key() for a in pc.plan.actions if a.kind is ActionKind.ADD}
            self.plans[plan.id] = PlanContext(plan, [], waiting=missing)
            return await self.apply(plan.id, confirm=True)
        except BaseException:
            # all or nothing: a refused removal leaves the device's rules as they were
            if changed:
                if own_before is None:
                    await self.store.delete_template(tid)
                else:
                    await self.store.put_template(own_before)
                if cfg is not before:
                    await self.update_device(before)
            raise

    def _remove_unmanaged(self, cfg: DeviceConfig, actions: list[PlanAction]) -> JobInfo:
        """Delete items another tool put on a device SyncMusic never synced. Not through the applier:
        that writes a manifest, and the device's first sync must still ask keep / wipe - so the session commits
        (iPod: its database) and the manifest the commit wrote goes again."""

        async def job(ctx: JobContext) -> dict[str, Any]:
            device = await self.open_device(cfg.id)
            report = SyncReport(
                id=uuid.uuid4().hex, device_id=cfg.id, message="removed in Player contents"
            )
            s = await device.begin()
            removed = 0
            try:
                for a in actions:
                    assert a.entry is not None
                    try:
                        await device.remove_track(s, track_ref(a.entry))
                        removed += 1
                    except Exception as e:  # noqa: BLE001 - reported like a sync failure
                        log.warning(
                            "%s: remove failed for %s (%s): %s",
                            cfg.id,
                            describe(a),
                            a.entry.path,
                            e,
                        )
                        report.failures.append(
                            ItemFailure(
                                ref=a.ref.key(),
                                title=a.title,
                                error=f"remove: {e}",
                                item=describe(a),
                                step="remove",
                                path=a.entry.path,
                            )
                        )
                    ctx.progress(removed / len(actions), a.title)
                await device.commit(s, Manifest(header=ManifestHeader(device_id=cfg.id)))
            except BaseException:
                await device.abort(s)
                raise
            finally:
                await asyncio.to_thread(manifest_io.delete, device.mount)
            report.counts = {"remove": removed}
            report.status = "partial" if report.failures else "succeeded"
            report.finished_at = dt.datetime.now(dt.UTC)
            await self.store.put_report(report, ctx.info.id)
            return report.model_dump(mode="json")

        return self.ctx.jobs.submit("sync", f"remove from {cfg.name}", job, device_id=cfg.id)

    async def _record_health(
        self,
        cfg: DeviceConfig,
        report: SyncReport | None = None,
        error: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """A write-speed sample of a full sync or an I/O error; a warning that is new now is
        announced once (`device.health_warning`)."""
        try:
            before = await self.health(cfg.id)
            if error is not None:
                await self.store.add_health(cfg.id, "io_error", detail=error[:300], job_id=job_id)
            elif report is not None and report.bytes_written >= storage_health.MIN_SAMPLE_BYTES:
                await self.store.add_health(
                    cfg.id, "write", report.bytes_written, report.write_seconds, job_id=job_id
                )
            after = await self.health(cfg.id)
        except Exception:  # noqa: BLE001 - bookkeeping never fails a sync
            log.exception("storage health of %s", cfg.id)
            return
        if after["warning"] and not before["warning"]:
            self.ctx.bus.publish(
                "device.health_warning", device_id=cfg.id, warning=after["warning"]
            )

    async def health(self, device_id: str) -> dict[str, Any]:
        """Overview → Storage health: I/O errors in 30 days, write speeds of the last syncs, the warning."""
        now = utcnow()
        records = await self.store.health(device_id, now - storage_health.WINDOW)
        return storage_health.summary(records, now)

    async def send_held_unstars(self, device_id: str, report_id: str) -> int:
        """The user confirms that the favourites a reverse sync held back as a suspected database reset were
        removed on purpose: they are queued as removals for their libraries (sent with the next reverse sync)
        and the report forgets them. Returns how many were queued."""
        await self.config(device_id)
        report = await self.store.report(report_id)
        if report is None or report.device_id != device_id:
            raise SyncError(f"unknown sync report {report_id!r}", "not_found")
        held = report.reverse.held_unstars if report.reverse else []
        if not held:
            return 0
        await self.store.capture_feedback(
            device_id, [("star", {"ref": k, "starred": False}) for k in held], [], None
        )
        assert report.reverse is not None
        report.reverse.held_unstars = []
        report.reverse.warnings.append(
            f"{len(held)} favourite removals confirmed: they are sent to the library with the next sync"
        )
        await self.store.put_report(
            report, (await self.store.report_jobs([report.id])).get(report.id)
        )
        log.info("%s: %d held favourite removals confirmed by the user", device_id, len(held))
        return len(held)

    async def undo_listening_changes(self, device_id: str, report_id: str) -> int:
        """Undo the favourites and ratings a sync set in the libraries: their previous values are queued for the
        libraries (sent with the next reverse sync) and the report forgets the changes. Returns how many."""
        await self.config(device_id)
        report = await self.store.report(report_id)
        if report is None or report.device_id != device_id:
            raise SyncError(f"unknown sync report {report_id!r}", "not_found")
        changes = report.reverse.changes if report.reverse else []
        if not changes:
            return 0
        rows: list[tuple[str, dict[str, Any]]] = [
            ("star", {"ref": c.ref, "starred": bool(c.previous)})
            if c.kind == "star"
            else ("rating", {"ref": c.ref, "rating": c.previous, "previous": c.value})
            for c in changes
        ]
        await self.store.capture_feedback(device_id, rows, [], None)
        assert report.reverse is not None
        report.reverse.changes = []
        report.reverse.warnings.append(
            f"{len(rows)} favourite and rating changes undone: the previous values are sent to the library "
            "with the next sync"
        )
        await self.store.put_report(
            report, (await self.store.report_jobs([report.id])).get(report.id)
        )
        log.info(
            "%s: %d listening-data changes of report %s undone", device_id, len(rows), report_id
        )
        return len(rows)

    async def acknowledge_health(self, device_id: str, undo: bool = False) -> dict[str, Any]:
        """Hide the storage warning until a newer I/O error or slow sync (`undo`: drop the latest acknowledgement)."""
        if undo:
            await self.store.delete_health(device_id, "ack", newest_only=True)
        else:
            await self.store.add_health(device_id, "ack")
        return await self.health(device_id)

    async def clear_health_errors(self, device_id: str) -> dict[str, Any]:
        """Forget the recorded I/O errors (the storage was replaced or reformatted); write speeds stay."""
        await self.store.delete_health(device_id, "io_error")
        await self.store.delete_health(device_id, "ack")
        return await self.health(device_id)

    async def remove_orphans(self, device_id: str) -> JobInfo:
        """Delete every orphan (on the device, gone from its library) as a sync job."""
        cfg = await self.config(device_id)
        keys = [r["key"] for r in await self.orphans(device_id)]
        if not keys:
            raise SyncError(f"{cfg.name} has no orphans", "invalid")
        return await self.remove_from_device(device_id, keys)

    async def reports(self, device_id: str | None = None) -> list[SyncReport]:
        return await self.store.reports(device_id)

    # --- on connect ------------------------------------------------------------------------------

    async def on_disk_added(self, cd: ConnectedDisk, startup: bool = False) -> None:
        """A disk appeared. `startup`: found by the daemon's start-up scan, plugged in before - then nothing starts
        (no on-connect sync or prompt, no update sync): only a real re-plug or the user starts one,
        the periodic update check runs on its normal interval from now."""
        if cd.device_id is None:
            return
        cfg = await self.store.device(cd.device_id)
        if cfg is None:
            return
        # the clock first, before anything mounts it; open_device waits for it
        if not cfg.mount_path and cd.device_id not in self._mounts:
            probe = registry.device_class(cfg.type)(
                cfg, self.ctx.config.mount_root / cfg.id, self.ctx
            )
            if probe.effective_capabilities().supports_time_sync:
                task = self._clock_tasks.get(cfg.id)
                if task is None or task.done():
                    task = asyncio.create_task(
                        self._set_clock(cfg, cd.disk.path), name=f"clock-{cfg.id}"
                    )
                    task.add_done_callback(functools.partial(self._clock_done, cfg.id))
                    self._clock_tasks[cfg.id] = task
        await self._check_fs(cfg, cd)
        if startup:
            log.info("%s: connected at start-up, no on-connect sync", cfg.id)
            return
        if cd.data_partition is None and not cfg.mount_path and not self._usb_mtp(cfg):
            # USB present, no medium / file system yet: the on-connect sync waits - the disk's
            # change event when the medium appears calls this again with the partition
            log.info("%s: USB disk present, storage not shared yet: on-connect sync waits", cfg.id)
            return
        outside = self.hold_automatic or not in_window(
            cfg.sync_windows, dt.datetime.now().astimezone()
        )
        if cfg.update_sync and cfg.on_connect != "auto":  # a full auto sync does all of it
            if outside:  # the periodic check runs it once the window opens
                log.info("%s: outside its sync windows, the update on connect waits", cfg.id)
                self._update_dirty.add(cfg.id)
            else:
                self._spawn_update(cfg.id, "on connect")
        if cfg.on_connect == "manual":
            return
        if cfg.on_connect == "ask":
            self._waiting[cfg.id] = {"reason": "on_connect: ask", "plan_id": None}
            self.ctx.bus.publish(
                "sync.needs_confirmation", device_id=cfg.id, reason="on_connect: ask"
            )
            return
        if outside:
            log.info(
                "%s: outside its sync windows, the on-connect sync waits for the next one", cfg.id
            )
            self._window_pending[cfg.id] = "on connect"
            return
        await self.auto_sync(cfg, "on connect")

    async def _check_fs(self, cfg: DeviceConfig, cd: ConnectedDisk) -> None:
        """A disk device that just connected is mounted and checked at once: mounted read-only (the kernel does
        that after errors) or a read error in its `probe_files` marks the file system damaged, so the device
        page and dashboard offer Repair now and no automatic sync starts on it."""
        cls = registry.device_class(cfg.type)
        if cls.network or cfg.mount_path or cd.data_partition is None or cfg.id in self._damaged:
            return
        try:
            device = await asyncio.wait_for(self.open_device(cfg.id), CONNECT_CHECK_TIMEOUT_S)
        except (SyncError, TimeoutError) as e:
            log.info("%s: no file system check on connect: %s", cfg.id, e)
            return
        except OSError as e:
            self.mark_damaged(cfg.id, e)
            return
        try:
            await asyncio.wait_for(
                asyncio.to_thread(probe_fs, device.mount, cls.probe_files),
                CONNECT_CHECK_TIMEOUT_S,
            )
        except TimeoutError:
            log.warning("%s: the file system check on connect timed out", cfg.id)
        except OSError as e:
            self.mark_damaged(cfg.id, e)

    async def _yield_update(self, device_id: str) -> None:
        """An update sync running on the device is cancelled and awaited, so the user's action goes ahead. Repeated
        while one is there: a schedule tick may spawn the next one while we wait for the last."""
        for _ in range(10):
            spawned = self._update_spawned.get(device_id)
            # by its task, not its status: a finished job still holds the device lock for a moment
            job = self.ctx.jobs.unfinished_for_device(device_id, "update")
            if job is None and (spawned is None or spawned.done()):
                return
            if spawned is not None and not spawned.done():  # started, job not yet submitted
                spawned.cancel()
                await asyncio.gather(spawned, return_exceptions=True)
            if job is not None:
                self.ctx.jobs.cancel(job.id)
                await self.ctx.jobs.wait(job.id)

    def _spawn_update(
        self, device_id: str, why: str, only_if_changed: bool = False, now: float | None = None
    ) -> None:
        # the interval restarts now, on the caller's clock (periodic_updates may run on a test clock)
        self._update_last[device_id] = time.monotonic() if now is None else now
        self._update_ran[device_id] = self._update_last[device_id]
        task = asyncio.create_task(
            self._auto_update(device_id, why, only_if_changed), name=f"update-{device_id}"
        )
        self._update_tasks.add(task)
        task.add_done_callback(self._update_tasks.discard)
        self._update_spawned[device_id] = task

    async def _auto_update(self, device_id: str, why: str, only_if_changed: bool) -> None:
        try:
            await self.update_sync(device_id, only_if_changed=only_if_changed)
        except SyncError as e:  # busy, not connected, ...: the next check tries again
            log.info("update sync of %s (%s) not started: %s", device_id, why, e)
            self._update_dirty.add(device_id)
        except Exception:  # noqa: BLE001 - logged, the service goes on
            log.exception("update sync of %s (%s)", device_id, why)

    async def mark_libraries_changed(
        self, library_ids: list[str] | None = None, *, except_device: str | None = None
    ) -> None:
        """Libraries changed (refreshed, or another device's plays/ratings/positions reached them; None: any): the
        devices that use them get an update check at the next tick."""
        for cfg in await self.store.devices():
            if cfg.id == except_device:
                continue
            if library_ids is None or set(library_ids) & set(await self.libraries_of(cfg)):
                self._update_dirty.add(cfg.id)

    async def periodic_updates(
        self, now: float | None = None, clock: dt.datetime | None = None
    ) -> list[str]:
        """The update checks that are due (on every schedule tick): a connected, idle device with update
        syncs on whose libraries changed, or whose interval passed. Returns the device ids checked."""
        now = time.monotonic() if now is None else now
        if self.hold_automatic:
            return []
        gap = (await self.store.app_settings()).update_change_gap_min * 60
        wall = dt.datetime.now().astimezone() if clock is None else clock
        due = []
        for cfg in await self.store.devices():
            if (
                not cfg.update_sync
                or not in_window(cfg.sync_windows, wall)  # changes stay marked until the window
                or not self._connected(cfg)
                or cfg.id in self._ejected
                or cfg.id in self._damaged
                or (self._waiting.get(cfg.id) or {}).get("plan_id")
                or self.ctx.jobs.device_busy(cfg.id)
            ):
                continue
            # first seen here (connected before the app started, a folder device): its interval starts now
            last = self._update_last.setdefault(cfg.id, now)
            interval = (cfg.update_every_min or 0) * 60
            # a library change waits for its slot: at most one change-triggered update per `gap`
            ran = self._update_ran.get(cfg.id)
            changed = cfg.id in self._update_dirty and (ran is None or now - ran >= gap)
            if changed or (interval and now - last >= interval):
                if not await self._reachable(cfg):  # the next slot tries again
                    self._update_last[cfg.id] = self._update_ran[cfg.id] = now
                    continue
                self._update_dirty.discard(cfg.id)
                self._spawn_update(cfg.id, "periodic check", only_if_changed=True, now=now)
                due.append(cfg.id)
        return due

    async def scheduled_syncs(self, now: dt.datetime | None = None) -> list[str]:
        """Start the daily syncs that are due (called every SCHEDULE_TICK_S by the app, `now` for tests):
        a device with `sync_at` whose time passed less than SCHEDULE_WINDOW ago today, not yet run today, connected,
        not ejected and idle (no job on it, no plan waiting for a confirmation; "on connect: ask" does not stop it). Returns the device ids started."""
        now = now or dt.datetime.now().astimezone()
        started: list[str] = []
        if self.hold_automatic:
            return started
        for cfg in await self.store.devices():
            if not in_window(cfg.sync_windows, now):
                continue  # the daily sync and a waiting on-connect sync need the window
            if cfg.id in self._window_pending and await self._start_pending(cfg):
                started.append(cfg.id)
                continue
            if not cfg.sync_at or self._scheduled_on.get(cfg.id) == now.date():
                continue
            hh, mm = (int(x) for x in cfg.sync_at.split(":"))
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if not due <= now < due + SCHEDULE_WINDOW:
                continue
            if (
                not self._connected(cfg)
                or cfg.id in self._ejected
                or cfg.id in self._damaged
                or (self._waiting.get(cfg.id) or {}).get("plan_id")  # a plan waits for the user
                or self.ctx.jobs.device_busy(cfg.id)
            ):
                continue  # tried again on the next tick while the window lasts
            self._scheduled_on[cfg.id] = now.date()
            if not await self._reachable(cfg):
                continue  # a server that does not answer: tomorrow's slot
            log.info("scheduled sync of %s (%s)", cfg.id, cfg.sync_at)
            self.ctx.bus.publish("sync.scheduled", device_id=cfg.id, at=cfg.sync_at)
            await self.auto_sync(cfg, f"scheduled at {cfg.sync_at}")
            started.append(cfg.id)
        return started

    def _window_wait(self, cfg: DeviceConfig) -> str | None:
        """ISO time when the device's next sync window opens, while it is outside them (None: inside, or none)."""
        now = dt.datetime.now().astimezone()
        if in_window(cfg.sync_windows, now):
            return None
        nxt = next_start(cfg.sync_windows, now)
        return nxt.isoformat() if nxt else None

    async def _start_pending(self, cfg: DeviceConfig) -> bool:
        """The on-connect sync that waited for the window, once the device is connected and idle;
        dropped when it was unplugged meanwhile."""
        if not self._connected(cfg):
            self._window_pending.pop(cfg.id, None)
            return False
        if (
            cfg.id in self._ejected
            or cfg.id in self._damaged
            or (self._waiting.get(cfg.id) or {}).get("plan_id")
            or self.ctx.jobs.device_busy(cfg.id)
        ):
            return False
        why = self._window_pending.pop(cfg.id)
        await self.auto_sync(cfg, f"{why}, in its sync window")
        return True

    async def auto_sync(self, cfg: DeviceConfig, why: str) -> JobInfo | None:
        """Plan and apply without asking (on connect with `on_connect: auto`, or the daily schedule): a plan that
        needs a confirmation or does not fit waits for the user instead."""
        if cfg.id in self._damaged:
            log.info("auto sync of %s (%s) not started: its file system is damaged", cfg.id, why)
            return None
        try:
            pc = await self.plan(cfg.id, auto=True)
        except Exception as e:  # noqa: BLE001 - reported as an event, the service goes on
            self.mark_damaged(cfg.id, e)
            log.warning("auto sync of %s (%s): plan failed: %s", cfg.id, why, e)
            if isinstance(e, SyncError) and e.code == "unmountable":
                self.ctx.bus.publish("device.error", device_id=cfg.id, code=e.code, error=str(e))
            self.ctx.bus.publish("sync.failed", device_id=cfg.id, error=str(e))
            return None
        plan = pc.plan
        if plan.needs_confirmation or plan.does_not_fit:
            reasons = plan.confirmation_reasons + (["does not fit"] if plan.does_not_fit else [])
            self._waiting[cfg.id] = {"reason": "; ".join(reasons), "plan_id": plan.id}
            self.ctx.bus.publish(
                "sync.needs_confirmation",
                device_id=cfg.id,
                plan_id=plan.id,
                reason="; ".join(reasons),
            )
            return None
        return await self.apply(plan.id)

    async def sync_all(self, mode: Literal["full", "update"] = "full") -> dict[str, Any]:
        """'Sync all connected': every connected, idle device with a library gets a full sync (a
        plan that needs a confirmation waits for it, like an automatic sync) or an update; the job runner's device
        limit queues them. Returns {devices, skipped: [{device_id, name, reason}]} at once; `sync_all.finished`
        {mode, done, waiting, failed} is published when every one has ended."""
        devices, skipped = [], []
        for d in (await self.list_devices())["registered"]:
            reason = (
                "not connected"
                if not d["connected"] or d["no_medium"]
                else "ejected"
                if d["ejected"] or d["ejecting"]
                else "busy"
                if d["busy"]
                else "no library"
                if d["no_libraries"]
                else "file system damaged"
                if d["fs_damaged"]
                else None
            )
            if reason is None and not await self._reachable(await self.config(d["id"])):
                reason = "not reachable"  # a network device whose server does not answer
            if reason:
                skipped.append({"device_id": d["id"], "name": d["name"], "reason": reason})
            else:
                devices.append(d["id"])

        async def one(device_id: str) -> str:
            try:
                if mode == "update":
                    job: JobInfo | None = await self.update_sync(device_id)
                else:
                    job = await self.auto_sync(await self.config(device_id), "sync all")
                if job is None:
                    return "waiting" if device_id in self._waiting else "failed"
                info = await self.ctx.jobs.wait(job.id)
                return "done" if info.status == JobStatus.SUCCEEDED else "failed"
            except Exception as e:  # noqa: BLE001 - counted; the device shows its error
                log.warning("sync all: %s: %s", device_id, e)
                return "failed"

        async def run() -> None:
            results = await asyncio.gather(*(one(i) for i in devices))
            self.ctx.bus.publish(
                "sync_all.finished",
                mode=mode,
                **{k: results.count(k) for k in ("done", "waiting", "failed")},
            )

        if devices:  # nothing started: no summary either
            task = asyncio.get_running_loop().create_task(run())
            self._update_tasks.add(task)
            task.add_done_callback(self._update_tasks.discard)
        return {"mode": mode, "devices": devices, "skipped": skipped}

    def mark_damaged(self, device_id: str | None, e: BaseException) -> bool:
        """`e` came from a read or write of the device: an EIO/EUCLEAN/EROFS in it marks the device's file system
        damaged. Notified once; automatic syncs stop until it is cleared. True when `e` is such an error."""
        err = fs_damage(e)
        if err is None or device_id is None:
            return err is not None
        if device_id not in self._damaged:
            why = err.strerror or os.strerror(err.errno or errno.EIO)
            self._damaged[device_id] = why
            log.warning(
                "%s: file system damaged (%s): automatic syncs stop until it is repaired",
                device_id,
                e,
            )
            self.ctx.bus.publish(
                "device.error",
                device_id=device_id,
                code="fs_damaged",
                error=f"the file system reports {why}: check and repair it, or wipe it",
            )
        return True

    def job_failed(self, info: JobInfo, e: BaseException) -> None:
        """Every failed job (JobRunner.on_failed): a device job that hit a damaged file system marks it."""
        self.mark_damaged(info.device_id, e)

    def _refuse_damaged(self, cfg: DeviceConfig) -> None:
        if cfg.id in self._damaged:
            raise SyncError(
                f"{cfg.name}: the file system is damaged ({self._damaged[cfg.id]}): check and repair it first",
                "fs_damaged",
            )

    async def repair(self, device_id: str) -> JobInfo:
        """Check and repair the data partition's file system (fsck), as a job: unmount, fsck (the guard
        allows the data partition only), mount again. Clears the damaged state when the tool succeeds."""
        cfg = await self.config(device_id)
        if cfg.mount_path or self._network(cfg):
            raise SyncError(f"{cfg.name} is not a disk: nothing to repair", "invalid")
        await self._yield_update(cfg.id)  # a user action wins over an update sync
        if self.ctx.jobs.device_busy(cfg.id):
            raise SyncError(f"{cfg.name} is busy", "busy")
        cd = self.detector.by_device(cfg.id)
        if cd is None or cd.data_partition is None:
            raise SyncError(f"{cfg.name} is not connected", "not_connected")
        if self.ctx.guard is None:
            raise SyncError("no block device guard configured", "invalid")
        guard, part = self.ctx.guard, cd.data_partition
        if foreign := await self.foreign_mounts(cfg):
            raise SyncError(
                f"{cfg.name} is also mounted by another program at {', '.join(foreign)}: "
                "unmount it everywhere first",
                "mounted_elsewhere",
            )

        async def job(ctx: JobContext) -> dict[str, Any]:
            ctx.progress(0.0, f"unmounting {cfg.name}")
            await self._unmount(cfg.id)
            self.drop_plans(cfg.id)
            ctx.progress(0.1, f"checking the file system on {part}")
            result = await mnt.repair(guard, part, registered=cfg.identity.guard_ids())
            self._damaged.pop(cfg.id, None)
            self._errors.pop(cfg.id, None)
            self._device_items.pop(cfg.id, None)
            ctx.progress(0.9, "errors corrected" if result.fixed else "no errors found")
            await self.open_device(cfg.id)
            self.ctx.bus.publish("device.repaired", device_id=cfg.id, fixed=result.fixed)
            return {"fixed": result.fixed, "output": result.output}

        return self.ctx.jobs.submit("repair", f"repair {cfg.name}", job, device_id=cfg.id)

    async def on_disk_removed(self, name: str) -> None:
        cd = self.detector.connected.get(name)
        await self.detector.disk_removed(name)
        if cd is not None and cd.device_id is not None:
            self._ejected.discard(cd.device_id)
            self._clock_set.pop(cd.device_id, None)
            task = self._clock_tasks.pop(cd.device_id, None)
            if task is not None and not task.done():
                task.cancel()
            foreign = self._foreign_tasks.pop(cd.device_id, None)
            if foreign is not None and not foreign.done():
                foreign.cancel()
            self._errors.pop(cd.device_id, None)
            self._damaged.pop(cd.device_id, None)  # re-plugged: try again
            self._waiting.pop(cd.device_id, None)
            self.drop_plans(cd.device_id)
            await self._close_mtp(cd.device_id)
            m = self._mounts.pop(cd.device_id, None)
            if m is not None:  # the kernel keeps the mount of a vanished disk
                try:
                    await mnt.drop_mount(m)
                except mnt.BlockdevError as e:
                    log.warning("dropping the mount of %s: %s", cd.device_id, e)


SCHEDULE_TICK_S = 30  # how often the app checks for due daily syncs
NETWORK_PROBE_S = 30  # a network device's login before an automatic start
NETWORK_UPDATE_EVERY_MIN = 360  # a new network device's update interval
SCHEDULE_WINDOW = dt.timedelta(hours=1)  # a daily sync starts at most this long after its time
VERIFY_SAMPLE = 0.1  # the share of items `verify: sample` reads back


def verify_sample(items: dict[str, ManifestEntry]) -> dict[str, ManifestEntry]:
    """A stable 10 % (at least one) of the manifest's items for `verify: sample`: the same keys every time, so
    a sample is not a lottery between syncs; the keys are spread by hash, not by name."""
    if not items:
        return {}
    n = max(1, math.ceil(len(items) * VERIFY_SAMPLE))
    keys = sorted(items, key=lambda k: hashlib.sha1(k.encode()).hexdigest())[:n]
    return {k: items[k] for k in keys}


def plan_summary(pc: PlanContext) -> dict[str, Any]:
    p = pc.plan
    data = p.model_dump(mode="json", exclude={"actions"})
    # big plans: a diverse sample of the items and the totals per group; the full list is GET /plans/{id}/items

    view = plan_view.sample(p)
    data["actions"] = view["actions"]
    data["space"]["dropped_detail"] = view["dropped_detail"]
    if data["limit"] is not None:
        data["limit"]["left_out"] = view["left_out"]
        data["limit"]["left_out_count"] = len(p.limit.left_out) if p.limit else 0
    data["totals"], data["bytes"], data["sampled"] = view["totals"], view["bytes"], view["sampled"]
    data["others"] = view["others"]  # kept items of other installations (the Keep card's sub-line)
    data["meta"] = plan_view.meta_view(p)  # the Metadata tab
    data["facets"] = plan_view.facets(p)  # the list's library / kind / rule filters
    data["counts"] = {k.value: p.count(k) for k in ActionKind}
    data["first_sync_matches"] = len(pc.matches)
    data["first_sync_existing"] = pc.existing
    data["orphans"] = sum(a.orphan for a in p.actions)  # kept: gone from their library
    # the plan review's Space now / after bars: the net bytes per media kind, and the bytes
    # written (new files and new versions, with their sidecars) for "Apply · write N"
    by_kind = {k.value: 0 for k in MediaKind}
    for a in p.actions:
        kind = a.item.track.kind if a.item else a.entry.kind if a.entry else MediaKind.MUSIC
        by_kind[kind.value] += a.size_delta
    data["by_kind_delta"] = by_kind
    data["write_bytes"] = sum(
        a.item.file.size + a.item.sidecar_bytes
        for a in p.actions
        if a.item and a.kind in (ActionKind.ADD, ActionKind.REPLACE)
    )
    # what the processors produce (the bytes the cache would take): no-cache mode is suggested above 2× the cache
    # limit
    data["processed_bytes"] = sum(
        a.item.file.size
        for a in p.actions
        if a.item and a.item.steps and a.kind in (ActionKind.ADD, ActionKind.REPLACE)
    )
    # transcode to fit: offered when bytes, not the device database limit, are what does not fit
    over_db = p.db is not None and p.db.level == "over"
    data["fit_targets"] = (
        [{"to": to, "bitrate_kbps": k} for to, k in FIT_TARGETS]
        if p.does_not_fit and not over_db
        else []
    )
    return data

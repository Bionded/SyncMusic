# SPDX-License-Identifier: GPL-3.0-or-later
"""Device base class."""

from __future__ import annotations

import asyncio
import shutil
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.base.context import AppContext
from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import UNKNOWN_SPACE as _UNKNOWN_SPACE
from syncmusic.core.models import (
    DeviceConfig,
    DeviceContents,
    DeviceFeedback,
    DeviceIdentity,
    DevicePlaylist,
    DeviceTrackRef,
    Manifest,
    MediaKind,
    PreparedItem,
    Track,
)
from syncmusic.core.profile import PROFILE_FIELDS, DeviceProfile, apply_profile, resolve_profile
from syncmusic.infra.blockdev.identify import BlockDeviceInfo


class ProbeResult(BaseModel):
    """Answer of Device.probe(): "this disk is mine"."""

    type_name: str
    model: str | None = None  # e.g. "classic_7g", "ipodmini2g"
    suggested_name: str = ""
    identity: DeviceIdentity = DeviceIdentity()
    confidence: int = 50  # higher wins when several classes claim a disk
    data_partition: str | None = None  # /dev path of the partition holding the data
    details: dict[str, Any] = {}


class DeviceSession:
    """State held between begin() and commit() (open device DB, inflight list ...)."""

    def __init__(self, manifest: Manifest | None = None) -> None:
        self.manifest = manifest
        self.inflight: list[str] = []  # relative paths written but not yet committed
        self.data: dict[str, Any] = {}


# capacity/free of a device that cannot tell its space (FTP without a quota): defined with SpaceReport, which
# serialises it as null; devices import it from here
UNKNOWN_SPACE = _UNKNOWN_SPACE


class Device(ABC):
    type_name: ClassVar[str]
    display_name: ClassVar[str]
    Settings: ClassVar[type[BaseModel]]
    capabilities: ClassVar[DeviceCapabilities]
    # The device keeps its own iPod_Control / .rockbox tree: a directory device (mount_path) of this
    # type may be the root of such a tree (core/mount_path.py).
    owns_device_tree: ClassVar[bool] = False
    # reached over the network (FTP): registered with its settings, never detected, mounted, formatted,
    # ejected or labelled; `mount` is a local placeholder folder
    network: ClassVar[bool] = False
    # an MTP player on USB (Android): also `network` (nothing to mount, format or label), but it is
    # detected by udev like a disk (infra/blockdev/mtp.py), connected only while plugged in, and eject ends the session
    usb_mtp: ClassVar[bool] = False
    # still in beta: Android/MTP): the device-types API flags it, the UI shows a Beta badge
    beta: ClassVar[bool] = False
    # file systems `format` can create, first = default; the UI offers a choice only when there are several
    # (values of infra/blockdev/format.py FILESYSTEMS)
    format_filesystems: ClassVar[tuple[str, ...]] = ("vfat",)
    # music only: podcasts and audiobooks never, whatever `capability_overrides` say (Rockbox)
    music_only: ClassVar[bool] = False
    # files (relative to the mount) read in full when the device connects: a read error there means the file
    # system is damaged, so it is flagged before a job writes to it (a player's own databases)
    probe_files: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def unavailable_reason(cls) -> str | None:
        """Why this type cannot work on this host (a missing system library), else None; an unavailable type is
        not offered at Register and cannot be registered."""
        return None

    def __init__(self, config: DeviceConfig, mount: Path, ctx: AppContext | None) -> None:
        self.config = config
        self.mount = mount
        self.ctx = ctx
        self.settings = self.Settings.model_validate(config.settings)
        # /dev path of the disk when mounted from a block device (set by the sync service); None = plain tree
        self.block_path: str | None = None

    @classmethod
    @abstractmethod
    def probe(cls, blk: BlockDeviceInfo | None, mount: Path | None) -> ProbeResult | None:
        """Is this disk (or mounted tree) a device of this class?"""

    @property
    def id(self) -> str:
        return self.config.id

    def detected_model(self) -> str | None:
        """Model id of this device in the known-models database; None = unknown."""
        return None

    def detected_profile(self) -> dict[str, Any]:
        """Profile fields the device reports itself (codecs, artwork, RTC ...)."""
        return {}

    def setting_profile(self) -> dict[str, Any]:
        """Profile fields the user chose in the device's settings."""
        return {}

    def profile(self, detect: bool = True) -> DeviceProfile:
        """Defaults → known model → detected → settings → overrides, each field with its source.
        `detect=False`: without asking the device (not connected) - no model, nothing detected."""
        overrides = {
            k: v for k, v in self.config.capability_overrides.items() if k in PROFILE_FIELDS
        }
        return resolve_profile(
            self.type_name,
            self.detected_model() if detect else None,
            self.capabilities,
            self.detected_profile() if detect else {},
            overrides,
            self.setting_profile(),
        )

    def model_capabilities(self) -> DeviceCapabilities:
        """Class capabilities refined by the device profile (impls add the rest: path rules, lyrics ...)."""
        return apply_profile(self.capabilities, self.profile())

    def effective_capabilities(self) -> DeviceCapabilities:
        caps = self.model_capabilities().with_overrides(self.config.capability_overrides)
        from syncmusic.devices.paths import (
            device_layout,
        )  # file devices' PathLayout under their base folder

        layout = device_layout(self.settings)
        if layout is not None:
            caps.path_layout = {k: layout.template(k) for k in MediaKind}
            caps.path_layout_multi_disc = layout.music_multi_disc
        if self.music_only:
            caps.supports_podcasts = caps.supports_audiobooks = caps.supports_video = False
        return caps

    @abstractmethod
    async def free_space(self) -> int: ...

    @abstractmethod
    async def capacity(self) -> int: ...

    @abstractmethod
    async def read_manifest(self) -> Manifest | None: ...

    async def write_manifest(self, manifest: Manifest) -> None:
        """Write the manifest alone, nothing else (a header change such as the home instance)."""
        await asyncio.to_thread(manifest_io.write, self.mount, manifest)

    async def read_state(self, name: str) -> bytes | None:
        """A small file of the app's state folder on the device (`.syncmusic/<name>`), None when absent."""
        path = self.mount / manifest_io.STATE_DIR / name
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            return None

    async def write_state(self, name: str, data: bytes) -> None:
        """Replace a file of the app's state folder on the device atomically."""
        await asyncio.to_thread(
            manifest_io.atomic_write, self.mount / manifest_io.STATE_DIR / name, data
        )

    async def delete_state(self, name: str) -> None:
        """Remove a file of the app's state folder on the device (no error when it is not there)."""
        await asyncio.to_thread((self.mount / manifest_io.STATE_DIR / name).unlink, True)

    @abstractmethod
    async def read_contents(self) -> DeviceContents: ...

    async def missing(self, manifest: Manifest) -> set[str]:
        """Manifest keys whose track is gone from the device (deleted outside SyncMusic); the planner
        adds them again. Default: none (devices that cannot check cheaply)."""
        return set()

    async def read_feedback(self, manifest: Manifest | None) -> DeviceFeedback:
        return DeviceFeedback()

    async def read_playlists(self, manifest: Manifest) -> dict[str, list[str]] | None:
        """The playlists on the device as name -> device-relative paths in order, for the reverse playlist sync
        ; None: the device's playlists cannot be edited on it (stock iPod) or read back."""
        return None

    async def feedback_warnings(self) -> list[str]:
        """Why the device records no listening data (a player setting is off); shown with plans and reverse
        reports. Default: none."""
        return []

    @abstractmethod
    async def begin(self) -> DeviceSession: ...

    @abstractmethod
    async def put_track(self, s: DeviceSession, item: PreparedItem) -> DeviceTrackRef: ...

    @abstractmethod
    async def remove_track(self, s: DeviceSession, ref: DeviceTrackRef) -> None: ...

    async def fetch_track(self, ref: DeviceTrackRef, dest: Path) -> bool:
        """Copy the item's file from the device to `dest` (a tags-only re-process starts from it).
        Default: the file under the mount; False when there is none (network devices: processed again)."""
        src = self.mount / ref.path
        if not ref.path or not await asyncio.to_thread(src.is_file):
            return False
        await asyncio.to_thread(shutil.copyfile, src, dest)
        return True

    async def rewrite_tags(
        self, s: DeviceSession, ref: DeviceTrackRef, item: PreparedItem
    ) -> DeviceTrackRef | None:
        """Tags-only re-process: update the item's file on the device in place from `item` (the
        device's file re-tagged). Default None: the applier removes and writes the file again."""
        return None

    async def update_metadata(self, s: DeviceSession, ref: DeviceTrackRef, track: Track) -> None:
        """Default: nothing to update in place (files carry their own tags)."""

    async def set_position(self, s: DeviceSession, ref: DeviceTrackRef, position_s: float) -> bool:
        """Move the item's bookmark to the library's position. Default: the device keeps no bookmarks
        that can be set (False)."""
        return False

    async def set_playlists(self, s: DeviceSession, playlists: list[DevicePlaylist]) -> None:
        """Default: no playlist support."""

    async def remove_playlists(self, s: DeviceSession, names: list[str]) -> None:
        """Remove playlists made on the device (named as `read_playlists` names them) once the reverse sync saved
        them to a library and no rule selects them. Called before `set_playlists`. Default:
        nothing (a device whose `read_playlists` is None never has any)."""

    async def reset_feedback(self, s: DeviceSession, consumed: DeviceFeedback) -> None:
        """Default: nothing to reset."""

    @abstractmethod
    async def commit(self, s: DeviceSession, manifest: Manifest) -> None:
        """Write device DB (if any), then the manifest."""

    async def abort(self, s: DeviceSession) -> None:
        """Discard an open session without committing."""

    async def check(self) -> None:
        """Network devices: reach the server and log in, raising with its reason. Default: nothing."""

    async def close(self) -> None:
        """Network devices: close the connection. Default: nothing."""

    async def format(self, name: str, fs: str | None = None) -> str | None:
        """Make a new file system and mount it at `self.mount`; returns the /dev path of the new data
        partition (None = unknown: the sync service then assumes the detected data partition)."""
        raise NotImplementedError(f"{self.type_name} cannot be formatted")

    async def rename(self, name: str) -> None:
        """Write `name` where the device keeps a name of its own inside its files (the iPod's database), on the
        mounted device. Default: nothing - the sync service changes the file system label (`label_for`)."""

    def label_for(self, name: str, fs: str | None, current: str | None) -> str | None:
        """The file system label a Rename writes for `name` (None: leave the label alone). Default: the
        name as the file system allows it, when SyncMusic can label that file system."""
        from syncmusic.infra.blockdev.format import LABEL_TOOLS, clean_label

        if fs not in LABEL_TOOLS:
            return None
        return clean_label(name, fs)  # type: ignore[arg-type]

    async def wipe(self) -> int | None:
        """Delete everything on the device (first sync "wipe"); returns the number of files deleted when it is known
        (the wipe job's report). Default: format with the device's name."""
        await self.format(self.config.name)
        return None

    async def set_time(self, now: datetime) -> None:
        raise NotImplementedError(f"{self.type_name} has no time sync")

    # devices whose database has a hard size limit (the iPod's iTunesDB); None = no limit, and then
    # db_size / estimate_db_size are not called
    db_size_limit: ClassVar[int | None] = None

    async def db_size(self) -> int | None:
        """Current size of the device database in bytes (None: there is none yet)."""
        return None

    async def estimate_db_size(self, items: int, playlist_entries: int) -> int:
        """Estimated database size with `items` tracks and `playlist_entries` entries in its playlists."""
        raise NotImplementedError

    def commit_reserve(self, manifest_items: int, caps: DeviceCapabilities | None = None) -> int:
        """Bytes needed on the device to commit (device DB + manifest) for this many items; `caps`: the effective
        capabilities when the caller already resolved them (per-item callers)."""
        caps = caps or self.effective_capabilities()
        return caps.db_overhead_bytes + 400 * manifest_items + 64 * 1024

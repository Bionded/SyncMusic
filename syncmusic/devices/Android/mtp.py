# SPDX-License-Identifier: GPL-3.0-or-later
"""Android player over MTP as a device (research in the developer notes).

MTP is no block device: nothing is mounted or formatted. udev finds the player on USB (infra/blockdev/mtp.py,
source `usb_mtp`), so it is listed among the found devices and registered from there (its USB serial prefilled),
or added by hand with its settings (the player's USB serial, the storage, a base folder); `network = True` is the
"no disk" flag. It is connected while udev sees it. It talks to the player through an `MtpBackend`: a few blocking
calls (list, read, send, delete, make folder, free space) run in a thread. The default backend is libmtp (devices/Android/_libmtp.py),
which opens only the player with the configured USB serial; when an allowlist is configured (tests/hardware.toml),
the serial must be in it. The tests use in-process fakes (tests/fake_mtp.py, tests/fake_libmtp.py).

Files go under `base_path` (default: the storage root) like a flash drive's tree, the manifest and the inflight list
under `<base_path>/.syncmusic/`, playlists as `.m3u8` with relative paths in `<base_path>/Playlists/`. MTP cannot
rename or overwrite: a file is deleted, then sent; the inflight list (written before every upload) lets the next
session remove what an interrupted one sent. No reverse sync: play counts, ratings and positions live in each
player app's private database, out of MTP's reach. Wipe deletes only the base folder's files.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes.util
import posixpath
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, field_validator

from syncmusic.base.capabilities import DeviceCapabilities, PathRules
from syncmusic.base.device import Device, DeviceSession, ProbeResult
from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import (
    DeviceContents,
    DeviceIdentity,
    DevicePlaylist,
    DeviceTrackRef,
    Manifest,
    PreparedItem,
)
from syncmusic.devices import _remote, paths
from syncmusic.devices.paths import PathLayout
from syncmusic.infra.blockdev.guard import GuardError
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from syncmusic.infra.blockdev.mtp import is_mtp, usb_addresses

T = TypeVar("T")
ROOT_SKIP = {
    "Android"
}  # with the storage's root as base folder: the system's app data folder is not walked
MANIFEST = manifest_io.MANIFEST_PATH.as_posix()
INFLIGHT = manifest_io.INFLIGHT_PATH.as_posix()


@dataclass
class MtpEntry:
    name: str
    is_dir: bool
    size: int = 0


@dataclass
class MtpStorage:
    name: str  # e.g. "Internal shared storage", "SD card"
    capacity: int
    free: int


class MtpBackend(Protocol):
    """One open MTP session to one player (blocking; the device calls it from a thread, one call at a time).
    Paths are POSIX paths relative to the root of `storage` (None = the first storage)."""

    def storages(self) -> list[MtpStorage]: ...

    def list(self, storage: str | None, path: str) -> list[MtpEntry]:
        """The folder's entries; FileNotFoundError when it does not exist."""
        ...

    def read(self, storage: str | None, path: str) -> bytes | None:
        """The file's content, None when it does not exist."""
        ...

    def send(self, storage: str | None, src: Path, path: str) -> None:
        """Copy a local file to `path` (its folder exists, no file there)."""
        ...

    def send_bytes(self, storage: str | None, data: bytes, path: str) -> None: ...

    def delete(self, storage: str | None, path: str) -> None:
        """Delete a file or an empty folder; missing is fine; OSError for a folder that is not empty."""
        ...

    def mkdir(self, storage: str | None, path: str) -> None:
        """Make a folder (its parent exists); existing is fine."""
        ...

    def close(self) -> None: ...


class MtpUnavailable(RuntimeError):
    """The player cannot be reached (not connected, held by another MTP client, no backend)."""


def _libmtp_backend(
    settings: AndroidMtpSettings, allowed: frozenset[str] | None = None
) -> MtpBackend:
    from syncmusic.devices.Android._libmtp import (
        LibMtpBackend,
    )  # Loads libmtp only when a player is used

    # udev's bus addresses → serials: only the wanted device (or allowlisted ones) is opened
    return LibMtpBackend(settings.usb_serial, usb=usb_addresses(), allowed=allowed)


class AndroidMtpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # the allowlist key: the player's USB serial (as libmtp reports it); the libmtp backend opens only a player
    # with this serial, and with an allowlist configured (tests/hardware.toml) only a listed one
    usb_serial: str
    storage: str | None = (
        None  # storage name ("Internal shared storage", "SD card"); empty = the first one
    )
    base_path: str = (
        ""  # folder on the storage our files go to ("" = its root); wipe removes only our files
    )
    formats: set[str] = {"mp3", "aac", "flac", "ogg", "opus"}
    layout: PathLayout = PathLayout()
    playlists: bool = True

    @field_validator("usb_serial")
    @classmethod
    def _serial(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("usb_serial must not be empty")
        return v

    @field_validator("base_path")
    @classmethod
    def _base(cls, v: str) -> str:
        # "", "/", "./": the storage's root; wipe then deletes only the files of our manifest
        parts = [p for p in v.replace("\\", "/").split("/") if p and p != "."]
        if ".." in parts:
            raise ValueError("base_path must not contain '..'")
        return "/".join(parts)


class AndroidMtp(Device):
    type_name = "android"
    display_name = "Android player (MTP)"
    network = True  # no disk to mount, format or label
    beta = True
    usb_mtp = True  # detected on USB by udev, connected while plugged in, eject ends the session
    Settings: ClassVar[type[BaseModel]] = AndroidMtpSettings
    capabilities: ClassVar[DeviceCapabilities] = DeviceCapabilities(
        formats={
            "mp3",
            "aac",
            "flac",
            "ogg",
            "opus",
        },  # what the common player apps play; no ALAC guarantee
        supports_podcasts=True,
        supports_audiobooks=True,
        lyrics="lrc_sidecar",
        path_rules=PathRules(max_path_len=240, max_name_len=255, name_len_unit="bytes"),
    )
    # settings -> an open backend; tests set a fake (on the class or the instance)
    backend_factory: Callable[..., MtpBackend] = staticmethod(_libmtp_backend)

    @classmethod
    def unavailable_reason(cls) -> str | None:
        # the libmtp backend needs the system library; a test fake needs nothing
        if cls.backend_factory is not _libmtp_backend or ctypes.util.find_library("mtp"):
            return None
        return "Android (MTP) is not available: libmtp is not installed (Debian/Ubuntu: apt install libmtp9t64)"

    def __init__(self, config, mount, ctx) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config, mount, ctx)
        self._backend: MtpBackend | None = None
        self._lock = threading.Lock()

    @property
    def s(self) -> AndroidMtpSettings:
        settings: AndroidMtpSettings = self.settings  # type: ignore[assignment]
        return settings

    @classmethod
    def probe(cls, blk: BlockDeviceInfo | None, mount: Path | None) -> ProbeResult | None:
        """An MTP player udev found (infra/blockdev/mtp.py): its model and USB serial."""
        if blk is None or not is_mtp(blk):
            return None
        name = " ".join(x for x in (blk.vendor, blk.model) if x) or "Android player"
        return ProbeResult(
            type_name=cls.type_name,
            model=blk.model,
            suggested_name=name,
            identity=DeviceIdentity(usb_serial=blk.serial),
            confidence=80,
            details={"usb_id": f"{blk.usb_vendor_id}:{blk.usb_product_id}"},
        )

    @classmethod
    def open_player(cls, usb_serial: str, allowed: frozenset[str] | None) -> MtpBackend:
        """A session to a found player that is not registered yet: Register's storage list and folder picker
        . Safety rule 1 as for a registered one; the caller closes it."""
        if allowed is not None and usb_serial not in allowed:
            raise GuardError(f"refusing MTP player {usb_serial!r}: not in the device allowlist")
        return cls.backend_factory(AndroidMtpSettings(usb_serial=usb_serial), allowed)

    def setting_profile(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if set(self.s.formats) != self.capabilities.formats:
            out["formats"] = set(self.s.formats)
        if not self.s.playlists:
            out["supports_playlists"] = False
        return out

    def label_for(self, name: str, fs: str | None, current: str | None) -> str | None:
        return None

    # --- backend ----------------------------------------------------------------------------------

    def _call(self, fn: Callable[[MtpBackend], T]) -> T:
        with self._lock:
            if self._backend is None:
                self._check_allowed()
                guard = self.ctx.guard if self.ctx is not None else None
                allowlist = guard.allowlist if guard is not None else None
                self._backend = self.backend_factory(
                    self.s, allowlist.serials if allowlist is not None else None
                )
            return fn(self._backend)

    def _check_allowed(self) -> None:
        """Safety rule 1 for a player that is no block device: with an allowlist, only a listed USB serial."""
        guard = self.ctx.guard if self.ctx is not None else None
        allowlist = guard.allowlist if guard is not None else None
        if allowlist is not None and self.s.usb_serial not in allowlist.serials:
            raise GuardError(
                f"refusing MTP player {self.s.usb_serial!r}: not in the device allowlist"
            )

    async def _run(self, fn: Callable[[MtpBackend], T]) -> T:
        return await asyncio.to_thread(self._call, fn)

    async def close(self) -> None:
        def work() -> None:
            with self._lock:
                if self._backend is not None:
                    self._backend.close()
                self._backend = None

        await asyncio.to_thread(work)

    async def check(self) -> None:
        """Open the player and make the base folder (registration / Test connection)."""

        def work(b: MtpBackend) -> None:
            self._storage(b)
            self._makedirs(b, "")

        await self._run(work)

    # --- file helpers (in the thread) ----------------------------------------------------------------

    def _abs(self, rel: str) -> str:
        return posixpath.join(self.s.base_path, rel) if rel else self.s.base_path

    def _storage(self, b: MtpBackend) -> MtpStorage:
        found = b.storages()
        if not found:
            raise MtpUnavailable("the player shows no storage (unlock it and allow file transfer)")
        if self.s.storage is None:
            return found[0]
        for st in found:
            if st.name == self.s.storage:
                return st
        names = ", ".join(st.name for st in found)
        raise MtpUnavailable(f"storage {self.s.storage!r} not on the player (it has: {names})")

    def _read(self, b: MtpBackend, rel: str) -> bytes | None:
        return b.read(self.s.storage, self._abs(rel))

    def _makedirs(self, b: MtpBackend, rel_dir: str) -> None:
        path = ""
        for part in PurePosixPath(self._abs(rel_dir)).parts:
            path = posixpath.join(path, part) if path else part
            b.mkdir(self.s.storage, path)

    def _delete(self, b: MtpBackend, rel: str) -> None:
        b.delete(self.s.storage, self._abs(rel))

    def _put(self, b: MtpBackend, rel: str, src: Path | bytes) -> None:
        """MTP has no overwrite or rename: delete what is there, then send."""
        self._makedirs(b, posixpath.dirname(rel))
        self._delete(b, rel)
        if isinstance(src, bytes):
            b.send_bytes(self.s.storage, src, self._abs(rel))
        else:
            b.send(self.s.storage, src, self._abs(rel))

    def _prune(self, b: MtpBackend, rel_dir: str) -> None:
        while rel_dir not in ("", "."):
            try:
                b.delete(self.s.storage, self._abs(rel_dir))
            except OSError:
                return  # not empty
            rel_dir = posixpath.dirname(rel_dir)

    def _walk(self, b: MtpBackend) -> dict[str, int]:
        """Every file under the base folder (relative path -> size), our state dir and hidden folders skipped."""
        out: dict[str, int] = {}

        def visit(rel_dir: str) -> None:
            try:
                entries = b.list(self.s.storage, self._abs(rel_dir))
            except FileNotFoundError:
                return
            for e in entries:
                if e.name.startswith(".") or (
                    not rel_dir and not self.s.base_path and e.name in ROOT_SKIP
                ):
                    continue
                rel = posixpath.join(rel_dir, e.name) if rel_dir else e.name
                if e.is_dir:
                    visit(rel)
                else:
                    out[rel] = e.size

        visit("")
        return out

    def _write_inflight(self, b: MtpBackend, rels: list[str]) -> None:
        if rels:
            self._put(b, INFLIGHT, "".join(r + "\n" for r in rels).encode("utf-8"))
        else:
            self._delete(b, INFLIGHT)

    # --- Device ------------------------------------------------------------------------------------

    async def free_space(self) -> int:
        return (await self._run(self._storage)).free

    async def capacity(self) -> int:
        return (await self._run(self._storage)).capacity

    async def read_manifest(self) -> Manifest | None:
        raw = await self._run(lambda b: self._read(b, MANIFEST))
        return None if raw is None else manifest_io.loads(raw.decode("utf-8"))

    async def missing(self, manifest: Manifest) -> set[str]:
        files = await self._run(self._walk)
        return {k for k, e in manifest.items.items() if e.path not in files}

    async def read_contents(self) -> DeviceContents:
        return _remote.contents(await self._run(self._walk))

    async def begin(self) -> DeviceSession:
        def work(b: MtpBackend) -> DeviceSession:
            raw = self._read(b, MANIFEST)
            manifest = None if raw is None else manifest_io.loads(raw.decode("utf-8"))
            s = DeviceSession(manifest)
            kept = {e.path for e in manifest.items.values()} if manifest else set()
            listed = (self._read(b, INFLIGHT) or b"").decode("utf-8", errors="replace")
            removed = [r for r in listed.splitlines() if r.strip() and r not in kept]
            for rel in removed:
                self._delete(b, rel)
            self._write_inflight(b, [])
            s.data["inflight_removed"] = removed
            _remote.fill_session(
                s, manifest, self._walk(b), self.effective_capabilities().path_rules
            )
            return s

        return await self._run(work)

    async def put_track(self, s: DeviceSession, item: PreparedItem) -> DeviceTrackRef:
        rules = self.effective_capabilities().path_rules
        rel, targets = _remote.track_targets(s, item, rules, self.s.layout)
        written = [d for _, d in targets]
        s.inflight += written

        def work(b: MtpBackend) -> None:
            self._write_inflight(
                b, s.inflight
            )  # listed before the upload: a crash still leaves them listed
            for src, dest in targets:
                self._put(b, dest, src)

        await self._run(work)
        _remote.track_written(s, item, rel, written, rules)
        size = await asyncio.to_thread(lambda: item.path.stat().st_size)
        return DeviceTrackRef(device_ref=rel, path=rel, size=size, sidecars=written[1:])

    async def remove_track(self, s: DeviceSession, ref: DeviceTrackRef) -> None:
        def work(b: MtpBackend) -> None:
            for rel in (ref.path, *ref.sidecars):
                self._delete(b, rel)
            self._prune(b, posixpath.dirname(ref.path))

        await self._run(work)
        s.data["taken"].discard(paths.path_key(ref.path, self.effective_capabilities().path_rules))

    async def set_playlists(self, s: DeviceSession, playlists: list[DevicePlaylist]) -> None:
        stale, texts = _remote.playlist_files(
            s, playlists, self.effective_capabilities().path_rules
        )

        def work(b: MtpBackend) -> None:
            for rel in stale:
                self._delete(b, rel)
            for rel, text in texts.items():
                self._put(b, rel, text)

        await self._run(work)

    async def read_state(self, name: str) -> bytes | None:
        rel = f"{manifest_io.STATE_DIR.as_posix()}/{name}"
        return await self._run(lambda b: self._read(b, rel))

    async def write_state(self, name: str, data: bytes) -> None:
        rel = f"{manifest_io.STATE_DIR.as_posix()}/{name}"

        def work(b: Any) -> None:
            self._put(b, rel, data)

        await self._run(work)

    async def delete_state(self, name: str) -> None:
        rel = f"{manifest_io.STATE_DIR.as_posix()}/{name}"

        def work(b: Any) -> None:
            self._delete(b, rel)

        await self._run(work)

    async def write_manifest(self, manifest: Manifest) -> None:
        data = manifest_io.dumps(manifest).encode("utf-8")

        def work(b: MtpBackend) -> None:
            self._put(b, MANIFEST, data)

        await self._run(work)

    async def commit(self, s: DeviceSession, manifest: Manifest) -> None:
        if "playlist_paths" in s.data:
            manifest.playlist_paths = {
                n: p for n, p in s.data["playlist_paths"].items() if n in manifest.playlists
            }
        data = manifest_io.dumps(manifest).encode("utf-8")

        def work(b: MtpBackend) -> None:
            self._put(b, MANIFEST, data)  # after every file: the manifest is written last
            self._write_inflight(b, [])

        await self._run(work)
        s.inflight.clear()
        s.manifest = manifest

    async def abort(self, s: DeviceSession) -> None:
        kept = {e.path for e in s.manifest.items.values()} if s.manifest else set()
        rels = [r for r in s.inflight if r not in kept]

        def work(b: MtpBackend) -> None:
            for rel in rels:
                self._delete(b, rel)
            self._write_inflight(b, [])

        await self._run(work)
        s.inflight.clear()

    async def wipe(self) -> int:
        """Delete every file under the base folder (the manifest too); the number of files besides the manifest."""

        def work(b: MtpBackend) -> int:
            files = self._walk(b)
            if (
                not self.s.base_path
            ):  # the storage's root: only what our manifest lists, never a files
                raw = self._read(b, MANIFEST)
                m = None if raw is None else manifest_io.loads(raw.decode("utf-8"))
                ours = {e.path for e in m.items.values()} if m else set()
                if m:
                    rules = self.effective_capabilities().path_rules
                    ours |= {
                        m.playlist_paths.get(n) or paths.playlist_path(n, rules)
                        for n in m.playlists
                    }
                files = {r: n for r, n in files.items() if r in ours}
            for rel in files:
                self._delete(b, rel)
            for rel in (MANIFEST, INFLIGHT):
                self._delete(b, rel)
            for folder in sorted({posixpath.dirname(r) for r in files}, key=len, reverse=True):
                self._prune(b, folder)
            with contextlib.suppress(OSError):
                b.delete(self.s.storage, self._abs(manifest_io.STATE_DIR.as_posix()))
            return len(files)

        return await self._run(work)

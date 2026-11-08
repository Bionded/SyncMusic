# SPDX-License-Identifier: GPL-3.0-or-later
"""SMB share as a device: files are written to a folder on a Windows/Samba share, like a flash
drive's tree. The network counterpart of the FTP device, with the SMB client of the folder library(`libraries/remote.py`).

A network device: never detected, registered with its settings (host, port, user/password or guest, the folder as
`/<share>/<folder…>`). The manifest and the inflight list live on the share under `<folder>/.syncmusic/` like on a
drive; files are written to a temporary name first and renamed into place, so an interrupted copy never looks like a
finished file. The free space is the share's (SMB reports it); `quota_gb`, if set, caps what SyncMusic may fill.
One SMB session per device (shared by every instance the sync service opens), used by one call at a time
(smbclient is blocking: calls run in a thread); a dropped session is opened again once.
The password is a secret: masked in the API, never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import posixpath
import shutil
import threading
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from syncmusic.base.capabilities import DeviceCapabilities, PathRules
from syncmusic.base.device import UNKNOWN_SPACE, Device, DeviceSession, ProbeResult
from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import (
    DeviceContents,
    DevicePlaylist,
    DeviceTrackRef,
    Manifest,
    PreparedItem,
)
from syncmusic.devices import _remote, paths
from syncmusic.devices.paths import PathLayout
from syncmusic.infra.blockdev.identify import BlockDeviceInfo

log = logging.getLogger(__name__)

T = TypeVar("T")
MANIFEST = manifest_io.MANIFEST_PATH.as_posix()
INFLIGHT = manifest_io.INFLIGHT_PATH.as_posix()
TMP_PREFIX = ".sdtmp-"
CHUNK = 1 << 20


def _missing(e: OSError) -> bool:
    """Not there (smbclient raises SMBOSError with errno ENOENT, not FileNotFoundError)."""
    return e.errno == errno.ENOENT


class _Connection:
    """The one SMB session of a device: shared by its instances, one call at a time."""

    def __init__(self) -> None:
        self.kw: dict[str, Any] | None = (
            None  # smbclient keyword arguments of the registered session
        )
        self.cache: dict[str, Any] = {}
        self.key = ""  # the settings it was opened with: other settings → a new session
        self.lock = threading.Lock()

    def close(self) -> None:
        if self.kw is not None:
            import smbclient

            with contextlib.suppress(Exception):  # the server may have dropped it already
                smbclient.reset_connection_cache(connection_cache=self.cache)
        self.kw, self.cache = None, {}


_CONNECTIONS: dict[str, _Connection] = {}  # device id → its session
_CONNECTIONS_LOCK = threading.Lock()


class SmbSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = Field(445, ge=1, le=65535)
    username: str = ""  # empty: guest
    password: SecretStr = SecretStr("")
    remote_path: str  # "/<share>/<folder…>": where the device's files go
    timeout_s: float = Field(30, gt=0)
    quota_gb: float | None = Field(
        None, gt=0
    )  # cap on what SyncMusic may fill; empty = the share's free space
    formats: set[str] = {"mp3", "aac", "flac", "ogg", "opus", "wav"}
    layout: PathLayout = PathLayout()
    playlists: bool = True

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip()
        if not v or "/" in v or "\\" in v or " " in v:
            raise ValueError("host must be a host name or IP address (no scheme, no path)")
        return v

    @field_validator("remote_path")
    @classmethod
    def _path(cls, v: str) -> str:
        parts = [p for p in v.replace("\\", "/").split("/") if p]
        if not parts:
            raise ValueError("remote_path must name a share: /<share>/<folder>")
        if any(p in (".", "..") for p in parts):
            raise ValueError("remote_path must not contain '.' or '..'")
        return "/" + "/".join(parts)


class SmbDevice(Device):
    type_name = "smb"
    display_name = "SMB share"
    network = True
    Settings: ClassVar[type[BaseModel]] = SmbSettings
    capabilities: ClassVar[DeviceCapabilities] = DeviceCapabilities(
        formats={"mp3", "aac", "flac", "ogg", "opus", "wav"},
        supports_podcasts=True,
        supports_audiobooks=True,
        lyrics="lrc_sidecar",
        # names Windows and Samba both take, at most 255 bytes per name including the temporary prefix
        path_rules=PathRules(
            max_path_len=240, max_name_len=255 - len(TMP_PREFIX), name_len_unit="bytes"
        ),
    )

    def __init__(self, config, mount, ctx) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config, mount, ctx)
        with _CONNECTIONS_LOCK:
            self._conn = _CONNECTIONS.setdefault(config.id, _Connection())

    @property
    def s(self) -> SmbSettings:
        settings: SmbSettings = self.settings  # type: ignore[assignment]
        return settings

    @classmethod
    def probe(cls, blk: BlockDeviceInfo | None, mount: Path | None) -> ProbeResult | None:
        return None  # a share is registered with its settings, never detected

    def setting_profile(self) -> dict[str, Any]:
        """Formats and playlists from the settings, where they differ from the class defaults."""
        out: dict[str, Any] = {}
        if set(self.s.formats) != self.capabilities.formats:
            out["formats"] = set(self.s.formats)
        if not self.s.playlists:
            out["supports_playlists"] = False
        return out

    def label_for(self, name: str, fs: str | None, current: str | None) -> str | None:
        return None  # nothing on the share carries the device's name

    # --- connection -------------------------------------------------------------------------------

    def _connect(self, conn: _Connection) -> dict[str, Any]:
        import smbclient

        s = self.s
        guest = not s.username
        kw: dict[str, Any] = {
            "username": s.username or "guest",
            "password": "" if guest else s.password.get_secret_value(),
            "port": s.port,
            "connection_cache": conn.cache,
        }
        if guest:  # a guest session cannot sign: the dialect check that needs it is off too
            smbclient.ClientConfig(require_secure_negotiate=False)
        smbclient.register_session(
            s.host,
            **kw,
            auth_protocol="ntlm",
            require_signing=not guest,
            encrypt=False,
            connection_timeout=int(s.timeout_s),
        )
        return kw

    def _call(self, fn: Callable[[dict[str, Any]], T]) -> T:
        """Run `fn` with the device's session (opened on first use or when the settings changed, opened again once
        after a dropped connection)."""
        from smbprotocol.exceptions import SMBConnectionClosed, SMBException

        conn = self._conn
        key = self.s.model_dump_json() + self.s.password.get_secret_value()
        with conn.lock:
            if conn.key != key:
                conn.close()
                conn.key = key
            for attempt in (1, 2):
                if conn.kw is None:
                    conn.kw = self._connect(conn)
                try:
                    return fn(conn.kw)
                except (SMBConnectionClosed, ConnectionError, TimeoutError) as e:
                    if attempt == 2:
                        raise
                    log.info("%s: SMB connection lost (%s), connecting again", self.id, e)
                    conn.close()
                except (
                    SMBException
                ) as e:  # a refusal: the share's reason, as an OSError the sync reports
                    raise OSError(f"smb://{self.s.host}{self.s.remote_path}: {e}") from e
            raise AssertionError("unreachable")

    async def _run(self, fn: Callable[[dict[str, Any]], T]) -> T:
        return await asyncio.to_thread(self._call, fn)

    async def close(self) -> None:
        """End the device's session (after a registration check; the next call opens a new one)."""

        def work() -> None:
            with self._conn.lock:
                self._conn.close()

        await asyncio.to_thread(work)

    async def check(self) -> None:
        """Log in and create / look at the folder (registration / Test connection); raises with the reason."""
        await self._run(lambda kw: self._makedirs(kw, ""))

    # --- remote file helpers (run in the thread, with the session) ------------------------------------

    def _unc(self, rel: str) -> str:
        parts = [p for p in self.s.remote_path.split("/") if p] + [p for p in rel.split("/") if p]
        return "\\\\" + self.s.host + "".join("\\" + p for p in parts)

    def _read(self, kw: dict[str, Any], rel: str) -> bytes | None:
        import smbclient

        try:
            with smbclient.open_file(self._unc(rel), mode="rb", **kw) as f:
                data: bytes = f.read()
                return data
        except OSError as e:
            if not _missing(e):
                raise
            return None

    def _makedirs(self, kw: dict[str, Any], rel_dir: str) -> None:
        import smbclient

        smbclient.makedirs(self._unc(rel_dir), exist_ok=True, **kw)

    def _put(self, kw: dict[str, Any], rel: str, write: Callable[[Any], None]) -> None:
        """Write to a temporary name in the same folder, then rename it into place."""
        import smbclient

        folder, name = posixpath.split(rel)
        self._makedirs(kw, folder)
        tmp = self._unc(posixpath.join(folder, TMP_PREFIX + name))
        with smbclient.open_file(tmp, mode="wb", **kw) as f:
            write(f)
        smbclient.replace(tmp, self._unc(rel), **kw)

    def _put_bytes(self, kw: dict[str, Any], rel: str, data: bytes) -> None:
        self._put(kw, rel, lambda f: f.write(data))

    def _delete(self, kw: dict[str, Any], rel: str) -> None:
        import smbclient

        try:
            smbclient.remove(self._unc(rel), **kw)
        except OSError as e:
            if not _missing(e):
                raise

    def _prune(self, kw: dict[str, Any], rel_dir: str) -> None:
        """Remove empty folders from `rel_dir` up to (not including) the device's folder."""
        import smbclient

        while rel_dir not in ("", "."):
            try:
                smbclient.rmdir(self._unc(rel_dir), **kw)
            except OSError:
                return  # not empty
            rel_dir = posixpath.dirname(rel_dir)

    def _walk(self, kw: dict[str, Any]) -> dict[str, int]:
        """Every file under the device's folder (relative path → size), our state dir and hidden folders skipped."""
        import smbclient

        out: dict[str, int] = {}

        def visit(rel_dir: str) -> None:
            try:
                entries = list(smbclient.scandir(self._unc(rel_dir), **kw))
            except OSError as e:
                if not _missing(e):
                    raise
                return
            for e in entries:
                if e.name.startswith("."):
                    continue
                rel = posixpath.join(rel_dir, e.name) if rel_dir else e.name
                if e.is_dir():
                    visit(rel)
                else:
                    out[rel] = int(e.smb_info.end_of_file)  # from the listing: no extra request

        visit("")
        return out

    def _inflight(self, kw: dict[str, Any]) -> list[str]:
        raw = self._read(kw, INFLIGHT)
        return [x for x in (raw or b"").decode("utf-8", errors="replace").splitlines() if x.strip()]

    def _write_inflight(self, kw: dict[str, Any], rels: list[str]) -> None:
        if rels:
            self._put_bytes(kw, INFLIGHT, "".join(r + "\n" for r in rels).encode("utf-8"))
        else:
            self._delete(kw, INFLIGHT)

    # --- Device ------------------------------------------------------------------------------------

    def _share_space(self, kw: dict[str, Any]) -> tuple[int, int]:
        """(total, free) bytes of the share."""
        import smbclient

        self._makedirs(kw, "")
        st = smbclient.stat_volume(self._unc(""), **kw)
        return int(st.total_size), int(st.caller_available_size)

    async def free_space(self) -> int:
        def work(kw: dict[str, Any]) -> int:
            _total, free = self._share_space(kw)
            if self.s.quota_gb is None:
                return free
            return min(free, max(0, int(self.s.quota_gb * 1000**3) - sum(self._walk(kw).values())))

        try:
            return await self._run(work)
        except OSError:
            return UNKNOWN_SPACE

    async def capacity(self) -> int:
        if self.s.quota_gb is not None:
            return int(self.s.quota_gb * 1000**3)
        try:
            return (await self._run(self._share_space))[0]
        except OSError:
            return UNKNOWN_SPACE

    async def read_manifest(self) -> Manifest | None:
        raw = await self._run(lambda kw: self._read(kw, MANIFEST))
        return None if raw is None else manifest_io.loads(raw.decode("utf-8"))

    async def missing(self, manifest: Manifest) -> set[str]:
        files = await self._run(self._walk)
        return {k for k, e in manifest.items.items() if e.path not in files}

    async def read_contents(self) -> DeviceContents:
        return _remote.contents(await self._run(self._walk))

    async def begin(self) -> DeviceSession:
        def work(kw: dict[str, Any]) -> DeviceSession:
            raw = self._read(kw, MANIFEST)
            manifest = None if raw is None else manifest_io.loads(raw.decode("utf-8"))
            s = DeviceSession(manifest)
            # what an interrupted sync copied but never committed (and its temporary files) goes first
            kept = {e.path for e in manifest.items.values()} if manifest else set()
            removed = [r for r in self._inflight(kw) if r not in kept]
            for rel in removed:
                self._delete(kw, rel)
            self._write_inflight(kw, [])
            files = self._walk(kw)
            for rel in [r for r in files if PurePosixPath(r).name.startswith(TMP_PREFIX)]:
                self._delete(kw, rel)
            s.data["inflight_removed"] = removed
            _remote.fill_session(s, manifest, files, self.effective_capabilities().path_rules)
            return s

        return await self._run(work)

    async def put_track(self, s: DeviceSession, item: PreparedItem) -> DeviceTrackRef:
        rules = self.effective_capabilities().path_rules
        rel, targets = _remote.track_targets(s, item, rules, self.s.layout)
        written = [d for _, d in targets]
        s.inflight += written

        def work(kw: dict[str, Any]) -> None:
            self._write_inflight(
                kw, s.inflight
            )  # listed before the copy: a crash still leaves them listed
            for src, dest in targets:
                with open(src, "rb") as local:
                    self._put(kw, dest, lambda f, local=local: shutil.copyfileobj(local, f, CHUNK))

        await self._run(work)
        _remote.track_written(s, item, rel, written, rules)
        size = await asyncio.to_thread(lambda: item.path.stat().st_size)
        return DeviceTrackRef(device_ref=rel, path=rel, size=size, sidecars=written[1:])

    async def remove_track(self, s: DeviceSession, ref: DeviceTrackRef) -> None:
        def work(kw: dict[str, Any]) -> None:
            for rel in (ref.path, *ref.sidecars):
                self._delete(kw, rel)
            self._prune(kw, posixpath.dirname(ref.path))

        await self._run(work)
        rules = self.effective_capabilities().path_rules
        s.data["taken"].discard(paths.path_key(ref.path, rules))

    async def set_playlists(self, s: DeviceSession, playlists: list[DevicePlaylist]) -> None:
        stale, texts = _remote.playlist_files(
            s, playlists, self.effective_capabilities().path_rules
        )

        def work(kw: dict[str, Any]) -> None:
            for rel in stale:
                self._delete(kw, rel)
            for rel, text in texts.items():
                self._put_bytes(kw, rel, text)

        await self._run(work)

    async def read_state(self, name: str) -> bytes | None:
        rel = f"{manifest_io.STATE_DIR.as_posix()}/{name}"
        return await self._run(lambda kw: self._read(kw, rel))

    async def write_state(self, name: str, data: bytes) -> None:
        rel = f"{manifest_io.STATE_DIR.as_posix()}/{name}"

        def work(kw: Any) -> None:
            self._put_bytes(kw, rel, data)

        await self._run(work)

    async def delete_state(self, name: str) -> None:
        rel = f"{manifest_io.STATE_DIR.as_posix()}/{name}"

        def work(kw: Any) -> None:
            self._delete(kw, rel)

        await self._run(work)

    async def write_manifest(self, manifest: Manifest) -> None:
        data = manifest_io.dumps(manifest).encode("utf-8")

        def work(kw: dict[str, Any]) -> None:
            self._put_bytes(kw, MANIFEST, data)

        await self._run(work)

    async def commit(self, s: DeviceSession, manifest: Manifest) -> None:
        if "playlist_paths" in s.data:
            manifest.playlist_paths = {
                n: p for n, p in s.data["playlist_paths"].items() if n in manifest.playlists
            }
        data = manifest_io.dumps(manifest).encode("utf-8")

        def work(kw: dict[str, Any]) -> None:
            self._put_bytes(kw, MANIFEST, data)  # after every file: the manifest is written last
            self._write_inflight(kw, [])

        await self._run(work)
        s.inflight.clear()
        s.manifest = manifest

    async def abort(self, s: DeviceSession) -> None:
        """Delete what this session copied (it never reached the manifest)."""
        kept = {e.path for e in s.manifest.items.values()} if s.manifest else set()
        rels = [r for r in s.inflight if r not in kept]

        def work(kw: dict[str, Any]) -> None:
            for rel in rels:
                self._delete(kw, rel)
            self._write_inflight(kw, [])

        await self._run(work)
        s.inflight.clear()

    async def wipe(self) -> int:
        """Delete every file under the device's folder (our state dir included: the manifest goes too); returns
        the number of files deleted besides the manifest."""

        def work(kw: dict[str, Any]) -> int:
            files = self._walk(kw)
            for rel in files:
                self._delete(kw, rel)
            for rel in (MANIFEST, INFLIGHT):
                self._delete(kw, rel)
            for folder in sorted({posixpath.dirname(r) for r in files}, key=len, reverse=True):
                self._prune(kw, folder)
            return len(files)

        return await self._run(work)

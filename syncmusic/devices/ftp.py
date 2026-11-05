# SPDX-License-Identifier: GPL-3.0-or-later
"""FTP server as a device: files are uploaded to a folder on the server, like a flash drive's tree.

A network device: never detected, registered with its settings (host, port, user/password or anonymous, base path,
passive mode, optional explicit FTPS). The manifest and the inflight list live on the server under
`<base_path>/.syncmusic/` like on a drive; uploads go to a temporary name first and are renamed into place, so an
interrupted upload never looks like a finished file. The space on the server is unknown to FTP: `quota_gb` (if
set) is what SyncMusic may fill, otherwise the plan does not check space. One connection per device (shared by
every instance the sync service opens: servers allow few sessions per client), used by one call at a time (ftplib
is blocking: calls run in a thread); a dropped connection is opened again once.
The password is a secret: masked in the API, never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import ftplib
import io
import logging
import posixpath
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
STATE = manifest_io.STATE_DIR.as_posix()  # ".syncmusic"
MANIFEST = manifest_io.MANIFEST_PATH.as_posix()
INFLIGHT = manifest_io.INFLIGHT_PATH.as_posix()
TMP_PREFIX = ".sdtmp-"
# errors after which the connection is opened again once (server closed it, timeout)
RECONNECT = (OSError, EOFError, ftplib.error_temp, ftplib.error_reply)


class _Connection:
    """The one FTP session of a device: shared by its instances, one call at a time."""

    def __init__(self) -> None:
        self.ftp: ftplib.FTP | None = None
        self.key = ""  # the settings it was opened with: other settings → a new session
        self.lock = threading.Lock()
        self.mlsd = True  # the server lists with MLSD; False: it refused it (vsftpd), LIST is parsed instead

    def close(self) -> None:
        if self.ftp is not None:
            with contextlib.suppress(OSError, EOFError, ftplib.Error):
                self.ftp.quit()
            with contextlib.suppress(OSError):
                self.ftp.close()
        self.ftp = None


_CONNECTIONS: dict[str, _Connection] = {}  # device id → its session
_CONNECTIONS_LOCK = threading.Lock()


class FtpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = Field(21, ge=1, le=65535)
    username: str = ""  # empty: anonymous login
    password: SecretStr = SecretStr("")
    base_path: str = "/"  # folder on the server the device's files go to
    passive: bool = True
    tls: bool = False  # explicit FTPS (AUTH TLS), data connections protected too
    timeout_s: float = Field(30, gt=0)
    quota_gb: float | None = Field(None, gt=0)  # space SyncMusic may fill; empty = not checked
    formats: set[str] = {"mp3", "aac", "flac", "ogg", "opus", "wav"}
    layout: PathLayout = PathLayout()
    playlists: bool = True

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip()
        if not v or "/" in v or " " in v:
            raise ValueError("host must be a host name or IP address (no scheme, no path)")
        return v

    @field_validator("base_path")
    @classmethod
    def _base(cls, v: str) -> str:
        parts = [p for p in v.replace("\\", "/").split("/") if p]
        if any(p in (".", "..") for p in parts):
            raise ValueError("base_path must not contain '.' or '..'")
        return "/" + "/".join(parts)


class FtpDevice(Device):
    type_name = "ftp"
    display_name = "FTP server"
    network = True
    Settings: ClassVar[type[BaseModel]] = FtpSettings
    capabilities: ClassVar[DeviceCapabilities] = DeviceCapabilities(
        formats={"mp3", "aac", "flac", "ogg", "opus", "wav"},
        supports_podcasts=True,
        supports_audiobooks=True,
        lyrics="lrc_sidecar",
        # names a Windows and a Linux server both take: no characters Windows refuses, at most 255 bytes of UTF-8
        # per name (ext4 & co.) including the upload's temporary prefix, no 4 GiB limit
        path_rules=PathRules(
            max_path_len=240, max_name_len=255 - len(".sdtmp-"), name_len_unit="bytes"
        ),
    )

    def __init__(self, config, mount, ctx) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config, mount, ctx)
        with _CONNECTIONS_LOCK:
            self._conn = _CONNECTIONS.setdefault(config.id, _Connection())

    @property
    def s(self) -> FtpSettings:
        settings: FtpSettings = self.settings  # type: ignore[assignment]
        return settings

    @classmethod
    def probe(cls, blk: BlockDeviceInfo | None, mount: Path | None) -> ProbeResult | None:
        return None  # a server is registered with its settings, never detected

    def setting_profile(self) -> dict[str, Any]:
        """Formats and playlists from the settings, where they differ from the class defaults."""
        out: dict[str, Any] = {}
        if set(self.s.formats) != self.capabilities.formats:
            out["formats"] = set(self.s.formats)
        if not self.s.playlists:
            out["supports_playlists"] = False
        return out

    def label_for(self, name: str, fs: str | None, current: str | None) -> str | None:
        return None  # nothing on the server carries the device's name

    # --- connection -------------------------------------------------------------------------------

    def _connect(self) -> ftplib.FTP:
        s = self.s
        ftp: ftplib.FTP = (
            ftplib.FTP_TLS(timeout=s.timeout_s) if s.tls else ftplib.FTP(timeout=s.timeout_s)
        )
        ftp.encoding = "utf-8"
        ftp.connect(s.host, s.port)
        user = s.username or "anonymous"
        ftp.login(user, s.password.get_secret_value() if s.username else "anonymous@")
        if isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()
        ftp.set_pasv(s.passive)
        return ftp

    def _call(self, fn: Callable[[ftplib.FTP], T]) -> T:
        """Run `fn` on the device's session (opened on first use or when the settings changed, opened again once
        after a dropped connection)."""
        conn = self._conn
        key = (
            self.s.model_dump_json()
        )  # the password counts too (SecretStr dumps masked: add its value)
        key += self.s.password.get_secret_value()
        with conn.lock:
            if conn.key != key:
                conn.close()
                conn.key, conn.mlsd = key, True
            for attempt in (1, 2):
                if conn.ftp is None:
                    conn.ftp = self._connect()
                try:
                    return fn(conn.ftp)
                except RECONNECT as e:
                    if attempt == 2:
                        raise
                    log.info("%s: FTP connection lost (%s), connecting again", self.id, e)
                    conn.close()
            raise AssertionError("unreachable")

    async def _run(self, fn: Callable[[ftplib.FTP], T]) -> T:
        return await asyncio.to_thread(self._call, fn)

    async def close(self) -> None:
        """End the device's session (after a registration check; the next call opens a new one)."""

        def work() -> None:
            with self._conn.lock:
                self._conn.close()

        await asyncio.to_thread(work)

    async def check(self) -> None:
        """Log in and look at the base folder (registration / Test connection); raises with the server's reason."""
        await self._run(lambda ftp: self._makedirs(ftp, ""))

    # --- remote file helpers (run in the thread, on the connection) ----------------------------------

    def _abs(self, rel: str) -> str:
        return posixpath.join(self.s.base_path, rel) if rel else self.s.base_path

    def _read(self, ftp: ftplib.FTP, rel: str) -> bytes | None:
        buf = io.BytesIO()
        try:
            ftp.retrbinary(f"RETR {self._abs(rel)}", buf.write)
        except ftplib.error_perm as e:
            if str(e).startswith("550"):
                return None
            raise
        return buf.getvalue()

    def _makedirs(self, ftp: ftplib.FTP, rel_dir: str) -> None:
        path = ""
        for part in PurePosixPath(self._abs(rel_dir)).parts[1:]:
            path += "/" + part
            with contextlib.suppress(
                ftplib.error_perm
            ):  # exists (or not allowed: the upload says why)
                ftp.mkd(path)

    def _put(self, ftp: ftplib.FTP, rel: str, data: io.BufferedIOBase | io.BytesIO) -> None:
        """Upload to a temporary name in the same folder, then rename it into place."""
        folder, name = posixpath.split(rel)
        self._makedirs(ftp, folder)
        tmp = self._abs(posixpath.join(folder, TMP_PREFIX + name))
        ftp.storbinary(f"STOR {tmp}", data)
        self._delete(ftp, rel)  # some servers refuse to rename onto an existing file
        ftp.rename(tmp, self._abs(rel))

    def _delete(self, ftp: ftplib.FTP, rel: str) -> None:
        with contextlib.suppress(ftplib.error_perm):  # not there
            ftp.delete(self._abs(rel))

    def _prune(self, ftp: ftplib.FTP, rel_dir: str) -> None:
        """Remove empty folders from `rel_dir` up to (not including) the base folder."""
        while rel_dir not in ("", "."):
            try:
                ftp.rmd(self._abs(rel_dir))
            except ftplib.error_perm:
                return  # not empty
            rel_dir = posixpath.dirname(rel_dir)

    def _walk(self, ftp: ftplib.FTP) -> dict[str, int]:
        """Every file under the base folder (relative path → size), our state dir and hidden folders skipped."""
        out: dict[str, int] = {}

        def visit(rel_dir: str) -> None:
            try:
                entries = self._list(ftp, self._abs(rel_dir))
            except ftplib.error_perm:
                return
            for name, facts in entries:
                if name in (".", "..") or name.startswith("."):
                    continue
                rel = posixpath.join(rel_dir, name) if rel_dir else name
                if facts.get("type") == "dir":
                    visit(rel)
                elif facts.get("type") == "file":
                    out[rel] = int(facts.get("size", 0))

        visit("")
        return out

    def _list(self, ftp: ftplib.FTP, path: str) -> list[tuple[str, dict[str, str]]]:
        """(name, {type: file|dir, size}) of a folder: MLSD where the server has it, else its Unix `LIST` lines."""
        if self._conn.mlsd:
            try:
                return list(ftp.mlsd(path, facts=["type", "size"]))
            except ftplib.error_perm as e:
                if not str(e).startswith(("500", "501", "502")):  # 550: no such folder
                    raise
                # vsftpd: "501 Option not understood" (OPTS MLST) / "500 Unknown command" (MLSD): no MLSD
                self._conn.mlsd = False
        lines: list[str] = []
        ftp.retrlines(f"LIST {path}", lines.append)
        out = []
        for line in lines:  # drwxr-xr-x 2 ftp ftp 4096 Oct 22 10:00 name with spaces
            parts = line.split(maxsplit=8)
            if len(parts) < 9 or parts[0][0] not in "-d":
                continue  # "total 12", links, devices
            kind = "dir" if parts[0][0] == "d" else "file"
            out.append((parts[8], {"type": kind, "size": parts[4]}))
        return out

    def _inflight(self, ftp: ftplib.FTP) -> list[str]:
        raw = self._read(ftp, INFLIGHT)
        return [x for x in (raw or b"").decode("utf-8", errors="replace").splitlines() if x.strip()]

    def _write_inflight(self, ftp: ftplib.FTP, rels: list[str]) -> None:
        if rels:
            self._put(ftp, INFLIGHT, io.BytesIO("".join(r + "\n" for r in rels).encode("utf-8")))
        else:
            self._delete(ftp, INFLIGHT)

    # --- Device ------------------------------------------------------------------------------------

    async def free_space(self) -> int:
        if self.s.quota_gb is None:
            return UNKNOWN_SPACE
        used = sum((await self._run(self._walk)).values())
        return max(0, int(self.s.quota_gb * 1000**3) - used)

    async def capacity(self) -> int:
        return UNKNOWN_SPACE if self.s.quota_gb is None else int(self.s.quota_gb * 1000**3)

    async def read_manifest(self) -> Manifest | None:
        raw = await self._run(lambda ftp: self._read(ftp, MANIFEST))
        return None if raw is None else manifest_io.loads(raw.decode("utf-8"))

    async def missing(self, manifest: Manifest) -> set[str]:
        files = await self._run(self._walk)
        return {k for k, e in manifest.items.items() if e.path not in files}

    async def read_contents(self) -> DeviceContents:
        return _remote.contents(await self._run(self._walk))

    async def begin(self) -> DeviceSession:
        def work(ftp: ftplib.FTP) -> DeviceSession:
            raw = self._read(ftp, MANIFEST)
            manifest = None if raw is None else manifest_io.loads(raw.decode("utf-8"))
            s = DeviceSession(manifest)
            # what an interrupted sync uploaded but never committed (and its temporary files) goes first
            kept = {e.path for e in manifest.items.values()} if manifest else set()
            removed = [r for r in self._inflight(ftp) if r not in kept]
            for rel in removed:
                self._delete(ftp, rel)
            self._write_inflight(ftp, [])
            files = self._walk(ftp)
            for rel in [r for r in files if PurePosixPath(r).name.startswith(TMP_PREFIX)]:
                self._delete(ftp, rel)
            s.data["inflight_removed"] = removed
            _remote.fill_session(s, manifest, files, self.effective_capabilities().path_rules)
            return s

        return await self._run(work)

    async def put_track(self, s: DeviceSession, item: PreparedItem) -> DeviceTrackRef:
        rules = self.effective_capabilities().path_rules
        rel, targets = _remote.track_targets(s, item, rules, self.s.layout)
        written = [d for _, d in targets]
        s.inflight += written

        def work(ftp: ftplib.FTP) -> None:
            self._write_inflight(
                ftp, s.inflight
            )  # listed before the upload: a crash still leaves them listed
            for src, dest in targets:
                with open(src, "rb") as f:
                    self._put(ftp, dest, f)

        await self._run(work)
        _remote.track_written(s, item, rel, written, rules)
        size = await asyncio.to_thread(lambda: item.path.stat().st_size)
        return DeviceTrackRef(device_ref=rel, path=rel, size=size, sidecars=written[1:])

    async def remove_track(self, s: DeviceSession, ref: DeviceTrackRef) -> None:
        def work(ftp: ftplib.FTP) -> None:
            for rel in (ref.path, *ref.sidecars):
                self._delete(ftp, rel)
            self._prune(ftp, posixpath.dirname(ref.path))

        await self._run(work)
        rules = self.effective_capabilities().path_rules
        s.data["taken"].discard(paths.path_key(ref.path, rules))

    async def set_playlists(self, s: DeviceSession, playlists: list[DevicePlaylist]) -> None:
        stale, texts = _remote.playlist_files(
            s, playlists, self.effective_capabilities().path_rules
        )

        def work(ftp: ftplib.FTP) -> None:
            for rel in stale:
                self._delete(ftp, rel)
            for rel, text in texts.items():
                self._put(ftp, rel, io.BytesIO(text))

        await self._run(work)

    async def commit(self, s: DeviceSession, manifest: Manifest) -> None:
        if "playlist_paths" in s.data:
            manifest.playlist_paths = {
                n: p for n, p in s.data["playlist_paths"].items() if n in manifest.playlists
            }
        data = manifest_io.dumps(manifest).encode("utf-8")

        def work(ftp: ftplib.FTP) -> None:
            self._put(
                ftp, MANIFEST, io.BytesIO(data)
            )  # after every file: the manifest is written last
            self._write_inflight(ftp, [])

        await self._run(work)
        s.inflight.clear()
        s.manifest = manifest

    async def abort(self, s: DeviceSession) -> None:
        """Delete what this session uploaded (it never reached the manifest)."""
        kept = {e.path for e in s.manifest.items.values()} if s.manifest else set()
        rels = [r for r in s.inflight if r not in kept]

        def work(ftp: ftplib.FTP) -> None:
            for rel in rels:
                self._delete(ftp, rel)
            self._write_inflight(ftp, [])

        await self._run(work)
        s.inflight.clear()

    async def wipe(self) -> int:
        """Delete every file under the base folder (our state dir included: the manifest goes too); returns the
        number of files deleted besides the manifest."""

        def work(ftp: ftplib.FTP) -> int:
            files = self._walk(ftp)
            for rel in files:
                self._delete(ftp, rel)
            for rel in (MANIFEST, INFLIGHT):
                self._delete(ftp, rel)
            for folder in sorted({posixpath.dirname(r) for r in files}, key=len, reverse=True):
                self._prune(ftp, folder)
            return len(files)

        return await self._run(work)

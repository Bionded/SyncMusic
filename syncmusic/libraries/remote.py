# SPDX-License-Identifier: GPL-3.0-or-later
"""A folder library on a server: the remote tree is read through a local **mirror** in the cache,
so the folder library scans it with the same code as a local folder.

- Media files are mirrored as *sparse tag copies*: the real size, with only the first `HEAD` and the last `TAIL`
  bytes downloaded - enough for mutagen's tags, embedded cover and duration (ID3/FLAC/Vorbis metadata sit at the
  start, an MP4 `moov` or an Ogg's last page at either end), never the whole library on the Pi's disk.
- Small files (sidecar covers, `.lrc`, playlists) are downloaded whole.
- A file is downloaded again only when its size or modification time on the server changed; files gone from the
  server go from the mirror. `full()` downloads one whole file for processing (the newest `KEEP_FULL` kept).

Read-only: nothing is ever written to the server. One connection per scan / download; the password is never logged.
`MirrorSource` keeps the mirror; `FtpSource` (`ftplib`, login or anonymous, optional explicit FTPS) and `SmbSource`
(`smbprotocol`, pure Python, no system mount; user or guest) only list, read a byte range and download a file.
"""

from __future__ import annotations

import contextlib
import ftplib
import json
import logging
import os
import posixpath
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

HEAD = 1 << 20  # first 1 MiB: tags incl. an embedded cover of usual size
TAIL = 256 << 10  # last 256 KiB: an MP4 moov or an Ogg's last page at the end
WHOLE_BELOW = HEAD + TAIL  # smaller files are downloaded whole
SMALL_EXTS = frozenset({".lrc", ".jpg", ".jpeg", ".png", ".m3u", ".m3u8"})
KEEP_FULL = 20
FULL_DIR = ".full"  # inside the mirror; hidden folders are not scanned
INDEX = ".index.json"  # rel → [size, mtime_ns, "sparse" | "whole"] of what the mirror holds

Stat = tuple[int, int]  # (size, mtime_ns)
Entry = tuple[str, bool, int, int]  # (name, is a folder, size, mtime_ns)


class RemoteError(OSError):
    """The server cannot be reached or read (the message names the reason, never the password)."""


def _mtime_ns(facts: dict[str, str], listed: str = "") -> int:
    """MLSD `modify` (YYYYMMDDHHMMSS[.fff], UTC) or a Unix LIST date ('Oct 22 10:00' / 'Oct 22 2025') → ns."""
    raw = facts.get("modify", "")
    if raw:
        whole, _, frac = raw.partition(".")
        try:
            d = datetime.strptime(whole, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            return 0
        return int(d.timestamp()) * 10**9 + int((frac or "0").ljust(9, "0")[:9])
    for fmt in ("%b %d %H:%M %Y", "%b %d %Y"):
        text = f"{listed} {datetime.now(UTC).year}" if fmt.startswith("%b %d %H") else listed
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=UTC).timestamp()) * 10**9
        except ValueError:
            continue
    return 0


class MirrorSource:
    """The mirror of a server folder; a protocol class adds `describe`, `session`, `entries`, `read_range`, `fetch`."""

    mirror: Path

    def describe(self) -> str:
        raise NotImplementedError

    def session(self) -> AbstractContextManager[Any]:
        """A logged-in connection (RemoteError with the reason when it cannot be opened)."""
        raise NotImplementedError

    def entries(self, h: Any, rel_dir: str) -> list[Entry]:
        """The folder's entries (RemoteError when it cannot be listed)."""
        raise NotImplementedError

    def read_range(self, h: Any, rel: str, offset: int, length: int) -> bytes:
        raise NotImplementedError

    def fetch(self, h: Any, rel: str, write: Callable[[bytes], Any]) -> None:
        """The whole file, chunk by chunk (RemoteError when it cannot be read)."""
        raise NotImplementedError

    # --- the library's view --------------------------------------------------------------------------

    def check(self) -> None:
        """Log in and list the folder; RemoteError with the reason."""
        with self.session() as h:
            self.entries(h, "")

    def walk(self) -> dict[str, Stat]:
        """Every non-hidden file below the folder: relative path → (size, mtime_ns). RemoteError when the server or
        the folder cannot be read (an empty result would make the planner delete everything from the device)."""
        out: dict[str, Stat] = {}
        with self.session() as h:

            def visit(rel_dir: str) -> None:
                for name, is_dir, size, mtime in sorted(self.entries(h, rel_dir)):
                    if name in (".", "..") or name.startswith("."):
                        continue
                    rel = posixpath.join(rel_dir, name) if rel_dir else name
                    if is_dir:
                        visit(rel)
                    else:
                        out[rel] = (size, mtime)

            visit("")
        return out

    def _index(self) -> dict[str, list[Any]]:
        try:
            data: dict[str, list[Any]] = json.loads((self.mirror / INDEX).read_text())
            return data
        except (OSError, ValueError):
            return {}

    def sync_mirror(self, files: dict[str, Stat], wanted: Callable[[str], bool]) -> None:
        """Bring the mirror to `files` (from `walk`): changed files of the kinds `wanted` names are fetched again
        (media sparse, small files whole), files no longer listed are deleted."""
        self.mirror.mkdir(parents=True, exist_ok=True)
        index = self._index()
        todo = [
            rel
            for rel, (size, mtime) in files.items()
            if wanted(rel) and index.get(rel, [None, None])[:2] != [size, mtime]
        ]
        for rel in set(index) - set(files):
            (self.mirror / rel).unlink(missing_ok=True)
            index.pop(rel)
        if todo:
            with self.session() as h:
                for rel in todo:
                    size, mtime = files[rel]
                    whole = size <= WHOLE_BELOW or os.path.splitext(rel)[1].lower() in SMALL_EXTS
                    dest = self.mirror / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        if whole:
                            with open(dest, "wb") as f:
                                self.fetch(h, rel, f.write)
                        else:
                            self._sparse(h, rel, size, dest)
                    except RemoteError as e:  # gone or unreadable since the listing: skipped
                        log.warning("%s: %s not mirrored: %s", self.describe(), rel, e)
                        dest.unlink(missing_ok=True)
                        index.pop(rel, None)
                        continue
                    os.utime(dest, ns=(mtime, mtime))
                    index[rel] = [size, mtime, "whole" if whole else "sparse"]
        (self.mirror / INDEX).write_text(json.dumps(index))

    def _sparse(self, h: Any, rel: str, size: int, dest: Path) -> None:
        head = self.read_range(h, rel, 0, HEAD)
        tail = self.read_range(h, rel, max(0, size - TAIL), TAIL)
        with open(dest, "wb") as f:
            f.truncate(size)  # a hole in between: no disk space for it
            f.write(head)
            f.seek(size - len(tail))
            f.write(tail)

    def full(self, rel: str) -> Path:
        """The whole file, downloaded into the mirror's `.full` folder (kept while it does not change)."""
        files = self._index()
        size, mtime = (files.get(rel) or [None, None])[:2]
        dest = self.mirror / FULL_DIR / rel
        if (
            dest.is_file()
            and size is not None
            and dest.stat().st_size == size
            and dest.stat().st_mtime_ns == mtime
        ):
            os.utime(dest, ns=(time.time_ns(), mtime))  # used now (atime): kept longest
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        with self.session() as h, open(part, "wb") as f:
            self.fetch(h, rel, f.write)
        part.replace(dest)
        if mtime is not None:
            os.utime(dest, ns=(time.time_ns(), mtime))
        self._prune_full(keep=dest)
        return dest

    def _prune_full(self, keep: Path) -> None:
        found = [p for p in (self.mirror / FULL_DIR).rglob("*") if p.is_file() and p != keep]
        found.sort(key=lambda p: p.stat().st_atime_ns, reverse=True)
        for p in found[KEEP_FULL - 1 :]:
            p.unlink(missing_ok=True)


class FtpSource(MirrorSource):
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        path: str,
        tls: bool,
        mirror: Path,
        timeout_s: float = 30,
    ) -> None:
        self.host, self.port, self.username, self.password = host, port, username, password
        self.path = "/" + "/".join(p for p in path.replace("\\", "/").split("/") if p)
        self.tls, self.mirror, self.timeout_s = tls, mirror, timeout_s
        self.mlsd = True

    def describe(self) -> str:
        return f"ftp://{self.host}:{self.port}{self.path}"

    @contextlib.contextmanager
    def session(self) -> Iterator[ftplib.FTP]:
        try:
            ftp: ftplib.FTP = (
                ftplib.FTP_TLS(timeout=self.timeout_s)
                if self.tls
                else ftplib.FTP(timeout=self.timeout_s)
            )
            ftp.encoding = "utf-8"
            ftp.connect(self.host, self.port)
            if self.username:
                ftp.login(self.username, self.password)
            else:
                ftp.login("anonymous", "anonymous@")
            if isinstance(ftp, ftplib.FTP_TLS):
                ftp.prot_p()
        except (OSError, EOFError, ftplib.Error) as e:
            raise RemoteError(f"{self.describe()}: {e}") from e
        try:
            yield ftp
        except (EOFError, ftplib.Error) as e:
            raise RemoteError(f"{self.describe()}: {e}") from e
        finally:
            with contextlib.suppress(OSError, EOFError, ftplib.Error):
                ftp.quit()
            with contextlib.suppress(OSError):
                ftp.close()

    def _abs(self, rel: str) -> str:
        return posixpath.join(self.path, rel) if rel else self.path

    def entries(self, h: ftplib.FTP, rel_dir: str) -> list[Entry]:
        """MLSD where the server has it, else its Unix LIST lines."""
        path = self._abs(rel_dir)
        try:
            if self.mlsd:
                try:
                    return [
                        (n, f.get("type") == "dir", int(f.get("size", 0)), _mtime_ns(f))
                        for n, f in h.mlsd(path, facts=["type", "size", "modify"])
                        if f.get("type") in ("dir", "file")
                    ]
                except ftplib.error_perm as e:
                    if not str(e).startswith(("500", "501", "502")):
                        raise
                    self.mlsd = False
            lines: list[str] = []
            h.retrlines(f"LIST {path}", lines.append)
        except ftplib.error_perm as e:
            raise RemoteError(f"{self.describe()}: {e}") from e
        out = []
        for line in lines:  # -rw-r--r-- 1 ftp ftp 4096 Oct 22 10:00 name with spaces
            parts = line.split(maxsplit=8)
            if len(parts) < 9 or parts[0][0] not in "-d":
                continue
            out.append(
                (parts[8], parts[0][0] == "d", int(parts[4]), _mtime_ns({}, " ".join(parts[5:8])))
            )
        return out

    def read_range(self, h: ftplib.FTP, rel: str, offset: int, length: int) -> bytes:
        """REST + RETR, the transfer cut off when enough has arrived."""
        chunks: list[bytes] = []
        got = 0
        try:
            # binary first - a listing leaves the session in ASCII, where pure-ftpd/vsftpd refuse REST
            h.voidcmd("TYPE I")
            conn = h.transfercmd(f"RETR {self._abs(rel)}", rest=offset or None)
        except ftplib.error_perm as e:
            raise RemoteError(f"{self.describe()}: {e}") from e
        try:
            while got < length:
                data = conn.recv(min(65536, length - got))
                if not data:
                    break
                chunks.append(data)
                got += len(data)
        finally:
            conn.close()
        with contextlib.suppress(ftplib.error_temp, ftplib.error_perm, ftplib.error_reply):
            h.voidresp()  # 226, or 426 / 451 for the transfer we cut off
        return b"".join(chunks)

    def fetch(self, h: ftplib.FTP, rel: str, write: Callable[[bytes], Any]) -> None:
        try:
            h.retrbinary(f"RETR {self._abs(rel)}", write)
        except ftplib.error_perm as e:
            raise RemoteError(f"{self.describe()}: {e}") from e


class SmbSource(MirrorSource):
    """`path` is `/<share>/<folder…>`. A guest (empty user name) logs in as `guest` without signing, which Samba
    and Windows refuse to sign for; each source has its own connection cache, so two logins to one server do not
    share a session."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        path: str,
        mirror: Path,
        timeout_s: float = 30,
    ) -> None:
        self.host, self.port, self.username, self.password = host, port, username, password
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        self.path = "/" + "/".join(parts)
        self.unc = "\\\\" + host + "".join("\\" + p for p in parts)
        self.mirror, self.timeout_s = mirror, timeout_s

    def describe(self) -> str:
        return f"smb://{self.host}:{self.port}{self.path}"

    @contextlib.contextmanager
    def session(self) -> Iterator[dict[str, Any]]:
        import smbclient
        from smbprotocol.exceptions import SMBException

        cache: dict[str, Any] = {}
        guest = not self.username
        kw: dict[str, Any] = {
            "username": self.username or "guest",
            "password": self.password if not guest else "",
            "port": self.port,
            "connection_cache": cache,
        }
        try:
            if guest:  # a guest session cannot sign: the dialect check that needs it is off too
                smbclient.ClientConfig(require_secure_negotiate=False)
            smbclient.register_session(
                self.host,
                **kw,
                auth_protocol="ntlm",
                require_signing=not guest,
                encrypt=False,
                connection_timeout=int(self.timeout_s),
            )
        except (OSError, ValueError, SMBException) as e:
            smbclient.reset_connection_cache(connection_cache=cache)
            raise RemoteError(f"{self.describe()}: {e}") from e
        try:
            yield kw
        except (ValueError, SMBException) as e:
            raise RemoteError(f"{self.describe()}: {e}") from e
        finally:
            with contextlib.suppress(Exception):  # the server may have dropped the session already
                smbclient.reset_connection_cache(connection_cache=cache)

    def _unc(self, rel: str) -> str:
        return self.unc + "".join("\\" + p for p in rel.split("/") if p)

    def entries(self, h: dict[str, Any], rel_dir: str) -> list[Entry]:
        import smbclient
        from smbprotocol.exceptions import SMBException

        try:
            out = []
            for e in smbclient.scandir(self._unc(rel_dir), **h):
                info = (
                    e.smb_info
                )  # what the listing brought: e.stat() would open another connection
                when = (
                    int(info.last_write_time.timestamp()) * 10**9
                    + info.last_write_time.microsecond * 1000
                )
                out.append((e.name, e.is_dir(), int(info.end_of_file), when))
            return out
        except (OSError, SMBException) as e:
            raise RemoteError(f"{self.describe()}: {e}") from e

    def read_range(self, h: dict[str, Any], rel: str, offset: int, length: int) -> bytes:
        import smbclient
        from smbprotocol.exceptions import SMBException

        try:
            with smbclient.open_file(self._unc(rel), mode="rb", **h) as f:
                f.seek(offset)
                data: bytes = f.read(length)
                return data
        except (OSError, SMBException) as e:
            raise RemoteError(f"{self.describe()}: {e}") from e

    def fetch(self, h: dict[str, Any], rel: str, write: Callable[[bytes], Any]) -> None:
        import smbclient
        from smbprotocol.exceptions import SMBException

        try:
            with smbclient.open_file(self._unc(rel), mode="rb", **h) as f:
                while chunk := f.read(1 << 20):
                    write(chunk)
        except (OSError, SMBException) as e:
            raise RemoteError(f"{self.describe()}: {e}") from e

# SPDX-License-Identifier: GPL-3.0-or-later
"""Client apps the server can hand out as updates: a release zip of an app (`<app>-<version>[-<variant>].zip`, one
top folder holding the app's files; the variant names a build, e.g. `hard`/`soft` float for the Compás app) found next to the SyncMusic binary or in the data dir's `tools/` folder. A paired client
that reports an older version of the same app gets the files of the newest zip and installs them itself."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import time
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from syncmusic.infra import self_update
from syncmusic.infra.self_update import running_binary
from syncmusic.infra.updates import Release, is_newer, running_version, version_key

log = logging.getLogger(__name__)

# the apps the server knows; a client names its app in `app` (its pairing info)
KNOWN_APPS = frozenset({"syncmusic-compas"})
MAX_FILE_BYTES = 64 * 1024 * 1024  # a zip member larger than this is not offered
# builds of an app; a client keeps getting its own build, one that does not say gets the first (recommended) one
VARIANTS = ("hard", "soft")
_NAME = re.compile(
    r"^(?P<app>[a-z0-9-]+?)-(?P<version>\d[0-9A-Za-z.+-]*?)(?:-(?P<variant>hard|soft))?\.zip$"
)


def variant_of(name: str | None) -> str:
    """The build a client asked for: a known variant, else the recommended one."""
    return name if name in VARIANTS else VARIANTS[0]


def _fits(zip_variant: str | None, want: str) -> bool:
    """A zip without a variant (older releases) fits every client."""
    return zip_variant is None or zip_variant == want


@dataclass(frozen=True)
class AppFile:
    name: str  # path inside the app folder, '/'-separated
    member: str  # path inside the zip
    size: int
    sha256: str


@dataclass(frozen=True)
class AppRelease:
    app: str
    version: str
    zip: Path
    files: tuple[AppFile, ...]
    variant: str | None = None

    def read(self, name: str) -> bytes | None:
        for f in self.files:
            if f.name == name:
                with zipfile.ZipFile(self.zip) as z:
                    return z.read(f.member)
        return None


def folders(data_dir: Path) -> list[Path]:
    """Where release zips may be: next to the SyncMusic binary, the data dir's tools folder."""
    out = [b.parent for b in (running_binary(),) if b is not None]
    return [*out, data_dir / "tools"]


def _safe(name: str) -> bool:
    parts = name.split("/")
    return bool(name) and not name.startswith("/") and all(p not in ("", ".", "..") for p in parts)


def read_zip(app: str, version: str, path: Path, variant: str | None = None) -> AppRelease | None:
    """The files of a release zip, None when it is not a usable app zip (no single top folder, unsafe names,
    too large members)."""
    try:
        with zipfile.ZipFile(path) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            tops = {i.filename.split("/", 1)[0] for i in infos}
            if len(tops) != 1 or not infos:
                return None
            top = tops.pop()
            files = []
            for i in infos:
                name = i.filename[len(top) + 1 :]
                if not _safe(name) or i.file_size > MAX_FILE_BYTES:
                    return None
                digest = hashlib.sha256(z.read(i)).hexdigest()
                files.append(AppFile(name, i.filename, i.file_size, digest))
    except (OSError, zipfile.BadZipFile):
        return None
    return AppRelease(app, version, path, tuple(sorted(files, key=lambda f: f.name)), variant)


_cache: dict[tuple[Path, float, int], AppRelease | None] = {}


def newest(app: str, data_dir: Path, variant: str | None = None) -> AppRelease | None:
    """The newest usable release zip of `app` in the search folders for a client of `variant` (see variant_of); of
    the same version a zip of exactly that variant wins over one without a variant."""
    if app not in KNOWN_APPS:
        return None
    want = variant_of(variant)
    found: list[tuple[str, Path, str | None]] = []
    for d in folders(data_dir):
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            m = _NAME.match(p.name)
            if (
                m
                and m["app"] == app
                and _fits(m["variant"], want)
                and version_key(m["version"]) is not None
                and p.is_file()
            ):
                found.append((m["version"], p, m["variant"]))
    found.sort(key=lambda f: (version_key(f[0]) or (), f[2] is not None), reverse=True)
    for version, p, var in found:
        st = p.stat()
        key = (p, st.st_mtime, st.st_size)
        if key not in _cache:
            _cache[key] = read_zip(app, version, p, var)
        if _cache[key] is not None:
            return _cache[key]
    return None


def offer(app: str, running: str, data_dir: Path, variant: str | None = None) -> AppRelease | None:
    """The release to offer a client that runs `running` of `app`: only a newer one."""
    rel = newest(app, data_dir, variant)
    return rel if rel is not None and is_newer(rel.version, running) else None


# --- download from SyncMusic's own GitHub release (Settings → Devices) -------------------------------------------


class AppDownloadError(Exception):
    """The app zip could not be downloaded or is not usable (the message says why); nothing was kept."""


def pick(
    releases: list[Release], app: str, variant: str | None = None
) -> tuple[str, dict[str, Any], str, str | None] | None:
    """(app version, zip asset, SHA256SUMS url, zip variant) of the newest app zip of `variant` any release offers."""
    want = variant_of(variant)
    found: list[tuple[str, dict[str, Any], str, str | None]] = []
    for r in releases:
        by_name = {str(a.get("name")): a for a in r.assets}
        sums = by_name.get("SHA256SUMS", {}).get("url")
        if not sums:
            continue
        for name, asset in by_name.items():
            m = _NAME.match(name)
            if (
                m
                and m["app"] == app
                and _fits(m["variant"], want)
                and version_key(m["version"]) is not None
            ):
                found.append((m["version"], asset, str(sums), m["variant"]))
    found.sort(key=lambda f: (version_key(f[0]) or (), f[3] is not None), reverse=True)
    return found[0] if found else None


async def install(
    releases: list[Release],
    app: str,
    data_dir: Path,
    transport: httpx.AsyncBaseTransport | None = None,
    variant: str | None = None,
) -> AppRelease:
    """Download the newest release zip of `app` (build `variant`) into `<data dir>/tools/`, checked against the release's SHA256SUMS
    and read back as a usable app zip before it is kept. Raises AppDownloadError (nothing is kept then)."""
    if app not in KNOWN_APPS:
        raise AppDownloadError(f"unknown app {app!r}")
    found = pick(releases, app, variant)
    if found is None:
        raise AppDownloadError(f"no SyncMusic release offers {app}")
    version, asset, sums, var = found
    folder = data_dir / "tools"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{app}-{version}-{var}.zip" if var else f"{app}-{version}.zip"
    part = folder / f"{name}.download"
    try:
        await self_update.download(asset, sums, part, transport)
        rel = read_zip(app, version, part, var)
        if rel is None:
            raise AppDownloadError(f"{name} is not a usable app zip: not installed")
        final = folder / name
        part.replace(final)
    except self_update.Refused as e:
        raise AppDownloadError(str(e)) from e
    except OSError as e:
        raise AppDownloadError(f"{name} not installed: {e}") from e
    finally:
        part.unlink(missing_ok=True)
    return AppRelease(app, version, final, rel.files, var)


RETRY_S = 3600  # a failed automatic download is tried again after this long


def own_release(releases: list[Release], running: str) -> list[Release]:
    """The release of the running server version (empty for a build no release matches)."""
    want = version_key(running)
    return [r for r in releases if want is not None and version_key(r.version) == want]


class AutoFetch:
    """Fetches the app builds of the server's own release by itself, so a connecting app is offered its update without
    anyone pressing Download. One download at a time; done once per server version, a failure is retried after
    RETRY_S and logged once."""

    def __init__(self) -> None:
        self.state = "idle"  # idle / downloading / done / failed / unavailable
        self.error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._failed_at: float | None = None
        self._logged: str | None = None

    def needed(self, held: AppRelease | None, client_version: str) -> bool:
        """True when the server holds nothing newer than the client and has not fetched its release's builds yet."""
        if self.state in ("done", "unavailable"):
            return False
        if self._failed_at is not None and time.monotonic() - self._failed_at < RETRY_S:
            return False
        return held is None or not is_newer(held.version, client_version)

    async def ensure(
        self,
        app: str,
        data_dir: Path,
        releases: Callable[[], Awaitable[list[Release]]],
        wait_s: float = 0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Start (or join) the download; wait at most `wait_s` for it, it carries on in the background after that."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(app, data_dir, releases, transport))
        if wait_s > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._task), wait_s)

    async def _run(
        self,
        app: str,
        data_dir: Path,
        releases: Callable[[], Awaitable[list[Release]]],
        transport: httpx.AsyncBaseTransport | None,
    ) -> None:
        self.state, self.error = "downloading", None
        running = running_version()
        try:
            own = own_release(await releases(), running)
            if not own:
                self.state = "unavailable"
                self.error = f"no SyncMusic release {running} to take the {app} builds from"
                self._log_once(self.error)
                return
            for variant in VARIANTS:
                try:
                    await install(own, app, data_dir, transport, variant)
                except AppDownloadError as e:
                    if variant == VARIANTS[0] or "no SyncMusic release" not in str(e):
                        raise
            self.state, self._failed_at = "done", None
            log.info("downloaded the %s builds of release %s", app, running)
        except (AppDownloadError, httpx.HTTPError, OSError, ValueError) as e:
            self.state, self._failed_at = "failed", time.monotonic()
            self.error = str(e) or type(e).__name__
            self._log_once(f"the {app} builds could not be downloaded: {self.error}")

    def _log_once(self, msg: str) -> None:
        if msg != self._logged:
            self._logged = msg
            log.warning("%s", msg)

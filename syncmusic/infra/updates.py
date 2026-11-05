# SPDX-License-Identifier: GPL-3.0-or-later
"""Updates from the project's GitHub releases: which version runs, the newest release of the chosen channel, its
notes and the download for this kind of install.

Releases: a tag `vX.Y.Z` is a stable release; every build of `main` is a pre-release `vX.Y.Z-N` (N = the build
number) that sorts after the stable release before it and before the stable `X.Y.Z` itself
(0.2.0 < 0.2.1-3 < 0.2.1-10 < 0.2.1). Channel `stable` only takes regular releases, `beta` takes both.

The check asks the GitHub API (unauthenticated, at most once a day plus on demand) with the ETag of the last
answer, so an unchanged list costs no rate limit; the last answer is kept in the cache dir. A failed check leaves
the state with an error, it never raises."""

from __future__ import annotations

import json
import logging
import os
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from packaging.version import InvalidVersion, Version

from syncmusic import USER_AGENT, __version__, _build

log = logging.getLogger(__name__)

APP_ROOT = Path(__file__).resolve().parents[2]  # the folder holding the `syncmusic` package
DEFAULT_REPO = "Bionded/SyncMusic"
API = "https://api.github.com/repos/{repo}/releases"


def releases_url(repo: str) -> str:
    """`owner/name` on GitHub, or a full http(s) URL of a releases list in GitHub's shape (a mirror)."""
    return repo if repo.startswith(("http://", "https://")) else API.format(repo=repo)


CHANNELS = ("stable", "beta")
MAX_NOTES = 30
TIMEOUT_S = 20
_TAG = re.compile(r"^v?(\d+(?:\.\d+)*)(?:-(\d+))?$")


def version_key(tag: str) -> tuple[Version, int, int] | None:
    """Sort key of a release tag (`v0.2.1`, `0.2.1-5`); None for a tag that is no version."""
    m = _TAG.match(tag.strip())
    if not m:
        return None
    try:
        base = Version(m.group(1))
    except InvalidVersion:
        return None
    build = m.group(2)
    return (base, 0, int(build)) if build is not None else (base, 1, 0)


def running_version() -> str:
    """This build's version: `0.2.1` for a release, `0.2.1-5` for build 5 of main."""
    return f"{__version__}-{_build.BUILD_NUMBER}" if _build.BUILD_NUMBER else __version__


def install_type() -> str:
    """How this copy was installed: desktop or standalone (the single-file binaries), service (the web binary as the
    systemd service of `syncmusic --install`, marked in its unit), docker, or source (a checkout, a venv install)."""
    if os.environ.get("SYNCMUSIC_INSTALL_TYPE") in (
        "desktop",
        "standalone",
        "docker",
        "service",
        "source",
    ):
        return os.environ["SYNCMUSIC_INSTALL_TYPE"]
    if _build.INSTALL_TYPE:
        return _build.INSTALL_TYPE
    if Path("/.dockerenv").exists() or os.environ.get("SYNCMUSIC_IN_DOCKER"):
        return "docker"
    return "source"


def running_commit(root: Path = APP_ROOT) -> str | None:
    """The commit this copy was built from, when known (`BUILD_COMMIT` next to the package)."""
    try:
        text = (root / "BUILD_COMMIT").read_text().split()
    except OSError:
        return None
    return text[0] if text else None


@dataclass
class Release:
    version: str  # the tag without the leading v
    prerelease: bool
    date: str | None  # ISO date of publishing
    notes: list[str]
    url: str | None  # the release page
    assets: list[dict[str, Any]]  # name, url, size


def _notes(body: str | None) -> list[str]:
    lines = []
    for line in (body or "").splitlines():
        line = line.strip().removeprefix("- ").removeprefix("* ").strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines[:MAX_NOTES]


def parse_releases(data: Any) -> list[Release]:
    """The published releases of a GitHub API answer, newest first; drafts and non-version tags are skipped."""
    out = []
    for r in data if isinstance(data, list) else []:
        if (
            not isinstance(r, dict)
            or r.get("draft")
            or version_key(str(r.get("tag_name", ""))) is None
        ):
            continue
        out.append(
            Release(
                version=str(r["tag_name"]).removeprefix("v"),
                prerelease=bool(r.get("prerelease")),
                date=(r.get("published_at") or "")[:10] or None,
                notes=_notes(r.get("body")),
                url=r.get("html_url"),
                assets=[
                    {
                        "name": a.get("name"),
                        "url": a.get("browser_download_url"),
                        "size": a.get("size"),
                    }
                    for a in r.get("assets") or []
                    if isinstance(a, dict)
                ],
            )
        )
    out.sort(key=lambda r: version_key(r.version) or (Version("0"), 0, 0), reverse=True)
    return out


def newest(releases: list[Release], channel: str) -> Release | None:
    """The newest release the channel takes: `stable` only regular releases, `beta` pre-releases too."""
    for r in releases:  # newest first
        if channel == "beta" or not r.prerelease:
            return r
    return None


def asset_for(release: Release, kind: str, machine: str | None = None) -> dict[str, Any] | None:
    """The download for this install: the desktop or web binary for this CPU; None for docker and source."""
    mode = {"desktop": "desktop", "standalone": "web", "service": "web"}.get(kind)
    if mode is None:
        return None
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(m := (machine or platform.machine()), m)
    for a in release.assets:
        name = str(a.get("name") or "")
        if name.startswith(f"syncmusic-{mode}-") and name.endswith(f"-linux-{arch}"):
            return a
    return None


def is_newer(candidate: str, running: str) -> bool:
    new, cur = version_key(candidate), version_key(running)
    return new is not None and (cur is None or new > cur)


@dataclass
class UpdateState:
    running: str = field(default_factory=running_version)
    commit: str | None = field(default_factory=running_commit)
    install_type: str = field(default_factory=install_type)
    channel: str = "stable"
    latest: str | None = None  # the newest version of the channel
    latest_date: str | None = None
    notes: list[str] = field(default_factory=list)
    url: str | None = None  # its release page
    asset: dict[str, Any] | None = None  # its download for this install
    sums: str | None = None  # its SHA256SUMS
    available: bool = False
    error: str | None = None
    checked: bool = False


class Checker:
    """Asks the releases API, reusing the last answer through its ETag (kept in `cache`)."""

    def __init__(self, cache: Path, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cache = cache
        self.transport = transport

    def _load(self) -> tuple[str | None, Any]:
        try:
            doc = json.loads(self.cache.read_text())
            return doc.get("etag"), doc.get("releases")
        except (OSError, ValueError, AttributeError):
            return None, None

    async def releases(self, repo: str) -> list[Release]:
        etag, cached = self._load()
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
        if etag and cached is not None:
            headers["If-None-Match"] = etag
        async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=self.transport) as c:
            r = await c.get(releases_url(repo), headers=headers, params={"per_page": 50})
        if r.status_code == 304 and cached is not None:
            return parse_releases(cached)
        r.raise_for_status()
        data = r.json()
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            self.cache.write_text(json.dumps({"etag": r.headers.get("etag"), "releases": data}))
        except OSError as e:
            log.debug("update cache not written: %s", e)
        return parse_releases(data)

    async def check(self, repo: str, channel: str) -> UpdateState:
        state = UpdateState(channel=channel if channel in CHANNELS else "stable", checked=True)
        try:
            found = newest(await self.releases(repo), state.channel)
        except httpx.HTTPStatusError as e:
            state.error = f"the releases cannot be read: HTTP {e.response.status_code}"
            return state
        except (httpx.HTTPError, OSError, ValueError) as e:
            state.error = f"the releases cannot be read: {type(e).__name__}"
            return state
        if found is None:
            return state
        state.latest, state.latest_date, state.url = found.version, found.date, found.url
        state.asset = asset_for(found, state.install_type)
        state.sums = next((a["url"] for a in found.assets if a.get("name") == "SHA256SUMS"), None)
        state.available = is_newer(found.version, state.running)
        if state.available:
            state.notes = found.notes
        return state

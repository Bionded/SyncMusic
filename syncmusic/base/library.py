# SPDX-License-Identifier: GPL-3.0-or-later
"""Library base class."""

from __future__ import annotations

import os
import re
import socket
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel

from syncmusic.base.capabilities import LibraryCapabilities
from syncmusic.base.context import AppContext
from syncmusic.core.models import HealthStatus, LibraryConfig, LibrarySnapshot, Lyrics
from syncmusic.core.rules.fields import ALL_FIELDS

if TYPE_CHECKING:
    import httpx


def _server_url(v: str) -> str:
    """A library server's base URL: http(s) with a host, no credentials or query in it - they would be shown by the
    API and in messages; credentials go into the username / password / token settings. The message does
    not repeat the URL. A value without a scheme (`10.0.0.10:4533`, `nas.local:13378`) gets `http://` - https
    is never guessed."""
    v = v.strip()
    if v and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", v):
        v = f"http://{v}"
    parts = urlsplit(v)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("must be an http:// or https:// URL")
    try:
        parts.port  # noqa: B018 - raises for a port that is not a number
    except ValueError:
        raise ValueError("has an invalid port") from None
    if parts.username is not None or parts.password is not None:
        raise ValueError(
            "must not contain a user name or password: use the library's credential settings"
        )
    if parts.query or parts.fragment:
        raise ValueError(
            "must not contain a query (?...) or fragment: use the library's credential settings"
        )
    return v


ServerUrl = Annotated[str, AfterValidator(_server_url)]


def error_reason(e: BaseException, timeout_s: float | None = None) -> str:
    """Why a request to a library server failed, never empty: some HTTP client errors (a connect timeout) have
    no message, which left "not reachable at <url>: " with nothing after it."""
    text = str(e).strip()
    name = type(e).__name__
    if isinstance(e, TimeoutError) or "Timeout" in name:
        after = f" after {timeout_s:g} s" if timeout_s else ""
        return f"timed out{after} ({name}{': ' + text if text else ''})"
    reason = _socket_reason(e)
    if reason is not None and reason not in text:
        # httpx says "All connection attempts failed"; the socket error says why ("Connection refused")
        return f"{text or name}: {reason}"
    return text or name


def _socket_reason(e: BaseException) -> str | None:
    """Reason of the innermost OSError in the exception's cause chain. A host lookup error (socket.gaierror)
    carries a getaddrinfo code, not an OS errno, so its own text is used (os.strerror(-2) is 'Unknown error -2';"""
    found: str | None = None
    seen: set[int] = set()
    cur: BaseException | None = e
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, socket.gaierror):
            found = cur.strerror or found
        elif isinstance(cur, OSError) and cur.errno and cur.errno > 0:
            found = os.strerror(cur.errno)
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return found


class LibraryUnavailable(OSError):
    """The library's server answered a download with a server error (5xx): the library is down, not the item
    ."""


def library_unavailable(e: BaseException) -> bool:
    """`e` (or its cause) says the library cannot be reached - connection refused or timed out, a host lookup
    failure, a dropped connection, a 5xx - rather than that one item is bad (damaged file)."""
    import httpx

    seen: set[int] = set()
    cur: BaseException | None = e
    while cur is not None and id(cur) not in seen:
        if isinstance(
            cur, LibraryUnavailable | socket.gaierror | ConnectionError | TimeoutError
        ) or (
            isinstance(cur, httpx.TransportError) and not isinstance(cur, httpx.UnsupportedProtocol)
        ):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False


class Library(ABC):
    type_name: ClassVar[str]
    display_name: ClassVar[str]
    display_hint: ClassVar[str] = ""  # e.g. the servers a type covers, shown next to its name
    # still in beta (Jellyfin): the library-types API flags it, the UI shows a Beta badge
    beta: ClassVar[bool] = False
    Settings: ClassVar[type[BaseModel]]
    capabilities: ClassVar[LibraryCapabilities]
    # A cached snapshot older than this is reloaded even if the state token is unchanged (for libraries whose
    # token cannot see every change, e.g. Navidrome ratings/play counts). None = trust the token.
    snapshot_max_age: ClassVar[timedelta | None] = None
    # rule fields this library's tracks have (core/rules/fields.py sets); the rule editor offers
    # only these for a device's libraries
    rule_fields: ClassVar[frozenset[str]] = ALL_FIELDS
    # the settings that sign in as one user: a device may set its own ("Sign in as another
    # user"); empty = the library has no per-user data (a folder)
    account_fields: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: LibraryConfig, ctx: AppContext | None) -> None:
        self.config = config
        self.ctx = ctx
        self.settings = self.Settings.model_validate(config.settings)
        # the device whose own account this instance signs in with; None = the library's account
        self.account: str | None = None

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def cache_key(self) -> str:
        """Where its snapshot is cached: the library id, `<id>@<device id>` for a device's own account."""
        return f"{self.config.id}@{self.account}" if self.account else self.config.id

    def failure_reason(self, e: BaseException) -> str:
        """Why a call to this library failed, never empty (a timeout names the library's timeout)."""
        timeout_s = getattr(self.settings, "timeout_s", None)
        return error_reason(e, timeout_s if isinstance(timeout_s, int | float) else None)

    @abstractmethod
    async def check(self) -> HealthStatus:
        """Reachable? Credentials ok?"""

    @abstractmethod
    async def state_token(self) -> str:
        """Cheap change detection: changes whenever the content changes."""

    @abstractmethod
    async def snapshot(self) -> LibrarySnapshot:
        """Full metadata load."""

    @abstractmethod
    async def open_media(self, item_id: str) -> AsyncIterator[bytes] | Path:
        """Original file: a local path, or an async byte stream."""

    async def open_cover(self, item_id: str) -> bytes | None:
        return None

    async def open_thumb(self, item_id: str, size: int) -> bytes | None:
        """A cover for a small thumbnail of `item_id` - a track, or an album / artist / playlist id where the
        library has pictures for those. Default: the track's cover; a server that can scale
        overrides it."""
        return await self.open_cover(item_id)

    async def get_lyrics(self, item_id: str) -> Lyrics | None:
        return None

    async def source_id(self) -> str | None:
        """A stable id of the source behind this library, the same whatever URL or mount path reaches it
        (e.g. `abs:<library uuid>`), so two instances can tell that their libraries are one. None = unknown;
        then libraries are matched by probing item ids."""
        return None

    async def request_rescan(self) -> bool:
        """Ask the server to rescan its files (its sizes/tags are out of date); True if a scan was started.
        Default: the library cannot."""
        return False

    async def close(self) -> None:
        """Release connections."""


async def body_chunks(response: httpx.Response, item_id: str) -> AsyncIterator[bytes]:
    """The streamed body of a media download, closed at the end. A body shorter than its `Content-Length` (the
    connection ended early) raises OSError: only the HTTP length proves a download complete - the library's own
    size can be stale (seen with Navidrome: kept the size of files replaced by smaller ones)."""
    expected = response.headers.get("content-length")
    encoded = response.headers.get("content-encoding", "identity") != "identity"
    size = 0
    try:
        async for chunk in response.aiter_bytes(1 << 20):
            size += len(chunk)
            yield chunk
    finally:
        await response.aclose()
    if expected and not encoded and size != int(expected):
        raise OSError(f"download of {item_id!r} ended early: got {size} of {expected} bytes")

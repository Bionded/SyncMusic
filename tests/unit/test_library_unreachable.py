"""a library server that does not answer (timeout) or refuses the connection gets a reason in check(),
not 'not reachable at <url>: ' with nothing after it."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import httpx
import pytest

from syncmusic.base.library import Library, error_reason
from syncmusic.core.models import LibraryConfig
from syncmusic.libraries.audiobookshelf import AudiobookshelfLibrary
from syncmusic.libraries.subsonic import SubsonicLibrary


@pytest.fixture
async def silent_url() -> AsyncIterator[str]:
    """A server that accepts the connection but never answers."""
    held: list[asyncio.StreamWriter] = []

    async def hold(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        held.append(w)

    srv = await asyncio.start_server(hold, "127.0.0.1", 0)
    yield f"http://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
    for w in held:
        w.close()
    srv.close()


@pytest.fixture
def closed_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _make(kind: str, url: str) -> Library:
    if kind == "navidrome":
        settings = {"url": url, "username": "u", "password": "pw", "timeout_s": 0.3}
        return SubsonicLibrary(
            LibraryConfig(id="nd", type=kind, name="ND", settings=settings), None
        )
    settings = {"url": url, "api_token": "tok", "timeout_s": 0.3}
    return AudiobookshelfLibrary(
        LibraryConfig(id="abs", type=kind, name="ABS", settings=settings), None
    )


@pytest.mark.parametrize("kind", ["navidrome", "audiobookshelf"])
async def test_timeout_is_named(kind, silent_url):
    status = await _make(kind, silent_url).check()
    assert not status.ok
    assert "not reachable at" in status.message and "timed out after 0.3 s" in status.message
    assert not status.message.rstrip().endswith(":")


@pytest.mark.parametrize("kind", ["navidrome", "audiobookshelf"])
async def test_refused_is_named(kind, closed_url):
    status = await _make(kind, closed_url).check()
    assert not status.ok
    assert "refused" in status.message.lower(), status.message


def test_unresolvable_host_has_no_unknown_error():
    """A getaddrinfo error number (negative EAI_*) is not an OS errno: os.strerror(-2) is 'Unknown error -2'."""
    try:
        try:
            raise socket.gaierror(-2, "Name or service not known")
        except socket.gaierror as cause:
            raise httpx.ConnectError("[Errno -2] Name or service not known") from cause
    except httpx.ConnectError as e:
        reason = error_reason(e, 30)
    assert "Name or service not known" in reason
    assert "Unknown error" not in reason, reason


@pytest.mark.parametrize("kind", ["navidrome", "audiobookshelf"])
async def test_unresolvable_host_is_named(kind):
    """the host lookup failure is the reason, without an appended 'Unknown error -2'."""
    status = await _make(kind, "http://no-such-host.invalid:4533").check()
    assert not status.ok
    assert "not reachable at" in status.message and "Unknown error" not in status.message
    assert not status.message.rstrip().endswith(":"), status.message

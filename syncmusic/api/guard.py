# SPDX-License-Identifier: GPL-3.0-or-later
"""Request guard against attacks through a browser.

The web UI is open on the LAN without a login (decision D12), so the guard stops what a foreign web page can do:
- DNS rebinding: the Host header must be an IP address, `localhost`, this machine's host name (also `.local`) or
  a name in `allowed_hosts` (`*` turns the check off, e.g. behind a reverse proxy with its own names);
- CSRF and cross-site WebSocket hijacking: a state-changing request (not GET/HEAD/OPTIONS) or a WebSocket whose
  `Origin` names another host than `Host`, or that the browser marks `Sec-Fetch-Site: cross-site`, is refused;
- huge bodies: a request body over `max_body` bytes is refused (413) before it fills the Pi's memory.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_BODY = 64 * 1024 * 1024  # a settings backup with every template is a few MB


def machine_names() -> set[str]:
    """This machine's host names, lower case, with and without `.local`."""
    names = {socket.gethostname().lower(), socket.getfqdn().lower()}
    short = {n.split(".")[0] for n in names if n}
    return {n for n in names | short | {s + ".local" for s in short} if n}


def _hostname(host: str) -> str:
    """`Host` header value without the port, lower case (IPv6 brackets removed)."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def host_allowed(host: str, allowed: set[str]) -> bool:
    if "*" in allowed:
        return True
    name = _hostname(host)
    if not name:
        return False
    if name == "localhost" or name in allowed:
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def origin_matches(origin: str, host: str) -> bool:
    """True when `Origin` names the same host and port as `Host` (`null` never matches)."""
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    return parts.netloc.lower() == host.strip().lower() or (
        # default ports may be left out of either header
        _hostname(parts.netloc) == _hostname(host)
        and ":" not in parts.netloc.rsplit("]", 1)[-1]
        and ":" not in host.rsplit("]", 1)[-1]
    )


class RequestGuard:
    """ASGI middleware; `allowed_hosts=None` skips the Host check (unit tests with Starlette's TestClient)."""

    def __init__(
        self, app: ASGIApp, allowed_hosts: set[str] | None = None, max_body: int = MAX_BODY
    ) -> None:
        self.app = app
        self.allowed = None if allowed_hosts is None else {h.lower() for h in allowed_hosts}
        self.max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "")
        if self.allowed is not None and not host_allowed(host, self.allowed):
            await self._refuse(scope, receive, send, 421, "Host not allowed (allowed_hosts)")
            return
        unsafe = scope["type"] == "websocket" or scope["method"] not in SAFE_METHODS
        if unsafe:
            origin = headers.get("origin")
            if headers.get("sec-fetch-site") == "cross-site" or (
                origin is not None and not origin_matches(origin, host)
            ):
                await self._refuse(scope, receive, send, 403, "Cross-site request refused")
                return
        if scope["type"] == "http":
            length = headers.get("content-length", "")
            if length.isdigit() and int(length) > self.max_body:
                await self._refuse(scope, receive, send, 413, "Request body too large")
                return
            receive = self._limited(receive)
        await self.app(scope, receive, send)

    def _limited(self, receive: Receive) -> Receive:
        """A `receive` that fails once a chunked body grows over `max_body`."""
        seen = 0

        async def limited() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_body:
                    raise _BodyTooLarge
            return message

        return limited

    async def _refuse(
        self, scope: Scope, receive: Receive, send: Send, code: int, text: str
    ) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await PlainTextResponse(text, status_code=code)(scope, receive, send)


class _BodyTooLarge(Exception):
    pass

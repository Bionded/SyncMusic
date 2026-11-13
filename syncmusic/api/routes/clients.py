# SPDX-License-Identifier: GPL-3.0-or-later
"""Wi-Fi clients: the client API (`/api/client/v1`: hello, pair, me, token rotation) and the routes the web UI uses
to pair, list, rename and revoke clients (`/api/v1/clients`).

The client API has its own authentication: `Authorization: Bearer <client token>`, valid only here and only for the
client's own device. It does not accept the web UI's session or API tokens, and client tokens are not accepted on
`/api/v1`. While Settings → Clients → Allow Wi-Fi clients is off, every client route answers `clients_disabled`.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import mimetypes
import socket
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit, urlunsplit

import segno
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from syncmusic import __version__
from syncmusic.core.client_sessions import (
    PAGE_MAX,
    AckRequest,
    AsList,
    ClientSession,
    FinishRequest,
    SyncRequest,
)
from syncmusic.devices.client import EVENTS_MAX, ClientEvent
from syncmusic.facade import AppFacade
from syncmusic.infra.announce import PROTOCOL_MAJOR
from syncmusic.infra.clients import ClientError, ClientInfo, show_code

PREFIX = "/api/client/v1"


def client_error(e: ClientError) -> JSONResponse:
    headers = {"Retry-After": str(e.retry_after_s)} if e.retry_after_s else None
    body = {
        "error": {
            "code": e.code,
            "message": str(e),
            "retry_after_s": e.retry_after_s,
            "details": {},
        }
    }
    return JSONResponse(body, status_code=e.status, headers=headers)


class PairRequest(BaseModel):
    code: str = Field(max_length=32)
    client: ClientInfo
    capabilities: dict[str, Any] = {}


class EventsRequest(BaseModel):
    events: Annotated[list[ClientEvent], AsList] = Field(max_length=EVENTS_MAX)


class RenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


def base_url(request: Request) -> str:
    """The URL a client should store: as the browser reached us (a reverse proxy's scheme via X-Forwarded-Proto)."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("host") or request.url.netloc
    prefix = request.scope.get("root_path", "").rstrip("/")
    return f"{proto}://{host}{prefix}/"


_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def _lan_ip() -> str | None:
    """This host's address on the default-route interface (a UDP connect sends no packet)."""
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("192.0.2.1", 9))
        ip = str(sock.getsockname()[0])
        if not ipaddress.ip_address(ip).is_loopback:
            return ip
    return None


def pairing_url(request: Request) -> str:
    """`base_url`, but a loopback host (UI opened via localhost) becomes the LAN address a phone can reach."""
    url = base_url(request)
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() in _LOOPBACK_NAMES
    if not loopback:
        return url
    ip = _lan_ip()
    if ip is None:
        return url
    netloc = f"{ip}:{parts.port}" if parts.port else ip
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def client_router(facade: AppFacade) -> APIRouter:
    r = APIRouter(prefix=PREFIX)
    app = facade.app
    sessions = app.client_sessions

    def session_answer(s: ClientSession) -> JSONResponse | dict[str, Any]:
        """200 with the plan when it is ready, 202 while planning or waiting for the user."""
        view = sessions.view(s)
        if s.status in ("planning", "needs_review"):
            return JSONResponse(view, status_code=202)
        return view

    def instance() -> dict[str, str]:
        return {"id": app.instance_id, "name": app.instance_name}

    async def allowed() -> None:
        if not app.client_settings.clients_allowed:
            raise ClientError(
                "clients_disabled", "Wi-Fi clients are not allowed on this server", 403
            )

    async def device(request: Request) -> str:
        await allowed()
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise ClientError("auth_required", "a client token is required", 401)
        return await app.clients.authenticate(token.strip())

    async def me_view(device_id: str) -> dict[str, Any]:
        p = await app.clients.get(device_id)
        return {
            "device_id": device_id,
            "name": p.name if p else "",
            "instance": instance(),
            "home": instance(),
            "poll_s": app.client_settings.client_poll_s,
        }

    @r.get("/hello")
    async def hello() -> dict[str, Any]:
        await allowed()
        return {
            "protocol": {"min": PROTOCOL_MAJOR, "max": PROTOCOL_MAJOR},
            "instance": instance(),
            "app_version": __version__,
            "pairing_open": await app.clients.pairing_open(),
            "features": [],
            "deprecations": [],
        }

    @r.post("/pair", status_code=201)
    async def pair(body: PairRequest, request: Request) -> dict[str, Any]:
        await allowed()
        address = request.client.host if request.client else "?"
        device_id, token, continued = await app.clients.pair(
            body.code, body.client, body.capabilities, address
        )
        p = await app.clients.get(device_id)
        await app.client_sessions.ensure_device(
            device_id, p.name if p else "Wi-Fi client", body.capabilities
        )
        app.bus.publish("client.paired", device_id=device_id, continued=continued)
        await app.announce()  # the code is used: `pair=0` once none is open
        return {
            "device_id": device_id,
            "token": token,
            "instance": instance(),
            "poll_s": app.client_settings.client_poll_s,
            "continued": continued,
            "settings": {},
        }

    @r.get("/me")
    async def me(device_id: str = Depends(device)) -> dict[str, Any]:
        return await me_view(device_id)

    @r.patch("/me")
    async def rename(body: RenameRequest, device_id: str = Depends(device)) -> dict[str, Any]:
        await app.clients.rename(device_id, body.name)
        app.bus.publish("client.changed", device_id=device_id)
        return await me_view(device_id)

    @r.get("/status")
    async def status(device_id: str = Depends(device)) -> dict[str, Any]:
        dev = await sessions.device(device_id)
        return sessions.status(device_id, dev, app.client_settings.client_poll_s)

    @r.post("/sync", response_model=None)
    async def start_sync(
        body: SyncRequest, device_id: str = Depends(device)
    ) -> JSONResponse | dict[str, Any]:
        s = await sessions.start(device_id, body)
        await asyncio.sleep(0)  # a quick plan answers at once
        return session_answer(s)

    @r.get("/sync/{session_id}", response_model=None)
    async def get_sync(
        session_id: str, device_id: str = Depends(device)
    ) -> JSONResponse | dict[str, Any]:
        return session_answer(sessions.get(device_id, session_id))

    @r.get("/sync/{session_id}/plan")
    async def plan_page(
        session_id: str,
        section: str,
        cursor: str | None = None,
        limit: int = PAGE_MAX,
        device_id: str = Depends(device),
    ) -> dict[str, Any]:
        return sessions.page(sessions.get(device_id, session_id), section, cursor, limit)

    @r.get("/files/{fid}", response_model=None)
    async def file(fid: str, request: Request, ticket: str | None = None) -> FileResponse:
        """With the bearer token, or with the `ticket` of the open session (the `url` of a fetch item)."""
        found: tuple[Path, str] | None
        if ticket:
            await allowed()
            hit = sessions.file_by_ticket(ticket, fid)
            found = None if hit is None else hit[1:]
        else:
            found = sessions.file_of(await device(request), fid)
        if found is None or not await asyncio.to_thread(found[0].is_file):
            raise ClientError("not_found", "no such file in an open sync session", 404)
        path, rel = found
        return FileResponse(
            path,
            media_type=mimetypes.guess_type(rel)[0] or "application/octet-stream",
            headers={"ETag": f'"{fid}"', "Cache-Control": "no-store"},
        )

    @r.post("/sync/{session_id}/ack")
    async def ack(
        session_id: str, body: AckRequest, device_id: str = Depends(device)
    ) -> dict[str, Any]:
        return await sessions.ack(device_id, session_id, body)

    @r.post("/sync/{session_id}/finish")
    async def finish(
        session_id: str, body: FinishRequest, device_id: str = Depends(device)
    ) -> dict[str, Any]:
        return await sessions.finish(device_id, session_id, body.status)

    @r.post("/events")
    async def events(body: EventsRequest, device_id: str = Depends(device)) -> dict[str, int]:
        """Listening data up: stored (deduplicated by eid), handed to the libraries shortly after or by the next sync."""
        dev = await sessions.device(device_id)
        result = await asyncio.to_thread(dev.add_events, body.events)
        deleted = [e.key for e in body.events if e.type == "deleted_by_user" and e.key]
        if deleted:  # the user deleted them on the client: no sync brings them back
            await app.sync.keep_off_device(device_id, deleted)
        if result["accepted"]:
            sessions.events_arrived(device_id)
        return result

    @r.post("/me/token/rotate")
    async def rotate(device_id: str = Depends(device)) -> dict[str, str]:
        return {"token": await app.clients.rotate(device_id)}

    return r


class CodeView(BaseModel):
    code: str
    display: str
    expires_at: str
    qr: str


def admin_router(facade: AppFacade) -> APIRouter:
    """`/api/v1/clients`: behind the optional login like the rest of `/api/v1`."""
    r = APIRouter(prefix="/clients")
    app = facade.app

    def code_view(code: Any, request: Request) -> dict[str, Any]:
        url = pairing_url(request)
        qr = f"syncmusic://pair?u={quote(url, safe='')}&c={code.code}&i={app.instance_id}&v={PROTOCOL_MAJOR}"
        return {
            "code": code.code,
            "display": show_code(code.code),
            "expires_at": code.expires_at.isoformat(),
            "url": url,
            "qr": qr,
            # dark on white whatever the theme: phone cameras read that best
            "qr_svg": segno.make(qr, error="m").svg_inline(scale=5, border=2, light="#fff"),
        }

    @r.get("")
    async def list_clients() -> dict[str, Any]:
        return {
            "allowed": app.client_settings.clients_allowed,
            "clients": [p.model_dump(mode="json") for p in await app.clients.list()],
        }

    @r.get("/pairings")
    async def list_codes(request: Request) -> list[dict[str, Any]]:
        return [code_view(c, request) for c in await app.clients.open_codes()]

    @r.post("/pairings", response_model=None)
    async def new_code(request: Request) -> JSONResponse | dict[str, Any]:
        if not app.client_settings.clients_allowed:
            return JSONResponse(
                {"error": "clients_disabled", "message": "allow Wi-Fi clients in Settings first"},
                status_code=409,
            )
        code = await app.clients.new_code(app.client_settings.client_code_minutes)
        await app.announce()
        return code_view(code, request)

    @r.delete("/pairings/{code}")
    async def cancel_code(code: str) -> dict[str, bool]:
        ok = await app.clients.cancel_code(code)
        await app.announce()
        return {"ok": ok}

    @r.get("/{device_id}", response_model=None)
    async def one_client(device_id: str) -> JSONResponse | dict[str, Any]:
        """A paired client for its device page: what it reported, and the progress of its open sync."""
        p = await app.clients.get(device_id)
        if p is None:
            return JSONResponse(
                {"error": "not_found", "message": "no such client"}, status_code=404
            )
        s = app.client_sessions.open_for(device_id)
        session = (
            {"id": s.id, "status": s.status, "message": s.message, "progress": s.progress()}
            if s
            else None
        )
        return p.model_dump(mode="json") | {"session": session}

    @r.patch("/{device_id}", response_model=None)
    async def rename(device_id: str, body: RenameRequest) -> JSONResponse | dict[str, Any]:
        if not await app.clients.rename(device_id, body.name):
            return JSONResponse(
                {"error": "not_found", "message": "no such client"}, status_code=404
            )
        app.bus.publish("client.changed", device_id=device_id)
        p = await app.clients.get(device_id)
        return p.model_dump(mode="json") if p else {}

    @r.post("/{device_id}/revoke", response_model=None)
    async def revoke(device_id: str) -> JSONResponse | dict[str, bool]:
        if not await app.clients.revoke(device_id):
            return JSONResponse(
                {"error": "not_found", "message": "no such client"}, status_code=404
            )
        app.bus.publish("client.revoked", device_id=device_id)
        return {"ok": True}

    return r

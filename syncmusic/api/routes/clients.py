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
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit, urlunsplit

import segno
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from syncmusic import __version__
from syncmusic.core.client_sessions import (
    PAGE_MAX,
    AckRequest,
    AsList,
    ClientSession,
    DiagnosticsRequest,
    FinishRequest,
    SyncRequest,
)
from syncmusic.devices.client import EVENTS_MAX, ClientEvent
from syncmusic.facade import AppFacade
from syncmusic.infra import client_apps
from syncmusic.infra.announce import PROTOCOL_MAJOR
from syncmusic.infra.clients import ClientError, ClientInfo, show_code
from syncmusic.infra.updates import is_newer

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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class UpdateAnswer(BaseModel):
    version: str = Field(max_length=64)
    answer: Literal["update", "later"]


class PairRequest(BaseModel):
    code: str = Field(max_length=32)
    client: ClientInfo
    capabilities: dict[str, Any] = {}


class PairAsk(BaseModel):
    client: ClientInfo
    capabilities: dict[str, Any] = {}


REQUEST_POLL_S = 3  # how often a client in registration mode asks whether the user answered
STATUS_WAIT_MAX = 30  # s: the longest a status long poll waits for a sync request


class TemplatesRequest(BaseModel):
    on: Annotated[list[str], AsList] = Field(max_length=500)


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
            "features": ["pair_requests"] if app.client_settings.client_pair_requests else [],
            "deprecations": [],
        }

    @r.post("/pair", status_code=201)
    async def pair(body: PairRequest, request: Request) -> dict[str, Any]:
        await allowed()
        address = request.client.host if request.client else "?"
        app.clients.throttle(address)
        device_id, token, continued = await app.clients.pair(
            body.code, body.client, body.capabilities, address
        )
        p = await app.clients.get(device_id)
        await app.client_sessions.ensure_device(
            device_id, p.name if p else "Wi-Fi client", body.capabilities, body.client.os
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

    async def requests_allowed() -> None:
        await allowed()
        if not app.client_settings.client_pair_requests:
            raise ClientError(
                "pair_requests_disabled", "pairing requests are off: use a pairing code", 403
            )

    @r.post("/pair-requests", status_code=202)
    async def pair_request(body: PairAsk, request: Request) -> dict[str, Any]:
        """Registration mode: ask the user to approve this client on the server."""
        await requests_allowed()
        address = request.client.host if request.client else "?"
        app.clients.throttle(address)
        req, secret = app.clients.new_request(
            body.client, body.capabilities, address, app.client_settings.client_code_minutes
        )
        app.bus.publish(
            "client.pair_request", request_id=req.id, status="pending", name=req.client.name
        )
        return {
            "request_id": req.id,
            "secret": secret,
            "display_code": req.display_code,
            "expires_at": req.expires_at.isoformat(),
            "poll_s": REQUEST_POLL_S,
            "instance": instance(),
        }

    @r.get("/pair-requests/{request_id}", response_model=None)
    async def pair_request_state(
        request_id: str, request: Request
    ) -> JSONResponse | dict[str, Any]:
        """202 while the user has not answered, 200 with the token once approved (handed out once)."""
        await requests_allowed()
        app.clients.throttle(request.client.host if request.client else "?")
        req = app.clients.request_for_client(request_id, request.headers.get("x-pair-secret", ""))
        if req.status == "pending":
            return JSONResponse({"status": "pending", "poll_s": REQUEST_POLL_S}, status_code=202)
        if req.status == "rejected":
            raise ClientError("request_rejected", "the pairing request was rejected", 403)
        return {
            "status": "approved",
            "device_id": req.device_id,
            "token": req.token,
            "instance": instance(),
            "poll_s": app.client_settings.client_poll_s,
            "continued": req.continued,
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
    async def status(wait: float = 0, device_id: str = Depends(device)) -> dict[str, Any]:
        """`wait` (s, at most 30): a long poll that answers as soon as the server asks for a sync."""
        if wait > 0:
            await app.sync.wait_client_sync_request(device_id, min(wait, STATUS_WAIT_MAX))
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
        return await sessions.page_wait(sessions.get(device_id, session_id), section, cursor, limit)

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

    async def templates_view(device_id: str) -> dict[str, Any]:
        cfg = await app.sync.config(device_id)
        on = cfg.rule_set.template_ids
        tpls = await app.store.templates()
        items = [{"id": t.id, "name": t.name, "on": t.id in on} for t in tpls.values()]
        items.sort(key=lambda x: (not x["on"], str(x["name"]).lower()))
        return {"templates": items}

    @r.get("/templates")
    async def templates(device_id: str = Depends(device)) -> dict[str, Any]:
        """The server's templates and which of them this client's device uses (switches only, no editing)."""
        return await templates_view(device_id)

    @r.put("/templates")
    async def set_templates(
        body: TemplatesRequest, device_id: str = Depends(device)
    ) -> dict[str, Any]:
        """Turn templates on and off for this client (the ids that are on; unknown ids are refused)."""
        known = await app.store.templates()
        bad = [i for i in body.on if i not in known]
        if bad:
            raise ClientError("not_found", f"no such template: {', '.join(bad)}", 404)
        cfg = await app.sync.config(device_id)
        keep = [i for i in cfg.rule_set.template_ids if i in body.on]  # their order stays
        ids = keep + [i for i in body.on if i not in keep]
        if ids != cfg.rule_set.template_ids:
            rs = cfg.rule_set.model_copy(update={"template_ids": ids})
            await app.sync.update_device(cfg.model_copy(update={"rule_set": rs}))
        return await templates_view(device_id)

    @r.get("/app-update")
    async def app_update(
        app_name: Annotated[str, Query(alias="app", max_length=64)],
        version: Annotated[str, Query(max_length=64)],
        device_id: str = Depends(device),
    ) -> dict[str, Any]:
        """A newer release of the client's own app the server holds, or `available: false`. The client installs
        the files itself (`url` relative to the client API)."""
        newest = await asyncio.to_thread(client_apps.newest, app_name, app.config.data_dir)
        offer = newest if newest is not None and is_newer(newest.version, version) else None
        client_newer = newest is not None and is_newer(version, newest.version)
        await app.clients.update_client(
            device_id,
            {
                "app_version": version,
                "update_offer": {"version": offer.version, "state": "offered", "at": _now_iso()}
                if offer is not None
                else None,
                "newer_than_server": client_newer,
            },
        )
        app.bus.publish("client.changed", device_id=device_id)
        base: dict[str, Any] = {
            "server_version": newest.version if newest is not None else None,
            "client_newer": client_newer,
        }
        if offer is None:
            return {"available": False, **base}
        return {
            "available": True,
            "app": offer.app,
            "version": offer.version,
            **base,
            "files": [
                {
                    "path": f.name,
                    "size": f.size,
                    "sha256": f.sha256,
                    "url": f"app-update/{quote(offer.app)}/{quote(offer.version)}/{quote(f.name)}",
                }
                for f in offer.files
            ],
        }

    @r.post("/app-update/answer")
    async def app_update_answer(
        body: UpdateAnswer, device_id: str = Depends(device)
    ) -> dict[str, Any]:
        """The user's answer on the client to the offered app update (shown on the device page)."""
        state = "accepted" if body.answer == "update" else "declined"
        await app.clients.update_client(
            device_id,
            {"update_offer": {"version": body.version, "state": state, "at": _now_iso()}},
        )
        app.bus.publish("client.changed", device_id=device_id)
        return {"ok": True}

    @r.get("/app-update/{app_name}/{version}/{name:path}", response_model=None)
    async def app_update_file(
        app_name: str, version: str, name: str, device_id: str = Depends(device)
    ) -> Response:
        rel = await asyncio.to_thread(client_apps.newest, app_name, app.config.data_dir)
        data = None
        if rel is not None and rel.version == version:
            data = await asyncio.to_thread(rel.read, name)
        if data is None:
            raise ClientError("not_found", "no such app file on this server", 404)
        return Response(
            data, media_type="application/octet-stream", headers={"Cache-Control": "no-store"}
        )

    @r.post("/diagnostics", status_code=202)
    async def diagnostics(
        body: DiagnosticsRequest, device_id: str = Depends(device)
    ) -> dict[str, Any]:
        """The user sent the app's log and state summary from the client (kept: the latest one)."""
        info = await asyncio.to_thread(sessions.save_diagnostics, device_id, body.log, body.summary)
        app.bus.publish("client.changed", device_id=device_id)
        return info

    @r.post("/sync/{session_id}/confirm")
    async def confirm(session_id: str, device_id: str = Depends(device)) -> dict[str, Any]:
        """The user confirmed on the client a plan that waits for confirmation (device setting)."""
        return await sessions.confirm(device_id, session_id)

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

    @r.get("/requests")
    async def list_requests() -> list[dict[str, Any]]:
        return [q.view() for q in app.clients.requests()]

    @r.post("/requests/{request_id}/{answer}", response_model=None)
    async def answer_request(request_id: str, answer: str) -> JSONResponse | dict[str, Any]:
        if answer not in ("approve", "reject"):
            return JSONResponse(
                {"error": "not_found", "message": "approve or reject"}, status_code=404
            )
        req = await app.clients.answer_request(request_id, answer == "approve")
        if req is None:
            return JSONResponse(
                {"error": "not_found", "message": "no such pairing request (expired?)"},
                status_code=404,
            )
        if req.device_id:
            p = await app.clients.get(req.device_id)
            await app.client_sessions.ensure_device(
                req.device_id,
                p.name if p else "Wi-Fi client",
                req.capabilities,
                req.client.os,
            )
            app.bus.publish("client.paired", device_id=req.device_id, continued=req.continued)
        else:
            app.bus.publish("client.pair_request", request_id=req.id, status=req.status)
        return req.view() | {"device_id": req.device_id}

    @r.get("/{device_id}", response_model=None)
    async def one_client(device_id: str) -> JSONResponse | dict[str, Any]:
        """A paired client for its device page: what it reported, and the progress of its open sync."""
        p = await app.clients.get(device_id)
        if p is None:
            return JSONResponse(
                {"error": "not_found", "message": "no such client"}, status_code=404
            )
        s = app.client_sessions.shown_for(device_id)
        session = (
            {
                "id": s.id,
                "status": s.status,
                "message": s.message,
                "plan_id": s.plan_id,
                "progress": s.progress(),
            }
            if s
            else None
        )
        diag = await asyncio.to_thread(app.client_sessions.diagnostics_info, device_id)
        dev = await app.client_sessions.device(device_id)
        outbox = await asyncio.to_thread(dev.outbox)
        return p.model_dump(mode="json") | {
            "outbox": {"files": len(outbox), "bytes": sum(outbox.values())},
            "session": session,
            "diagnostics": diag,
            "sync_requested": app.sync.sync_requested(device_id),
        }

    @r.post("/{device_id}/sync", response_model=None)
    async def request_sync(device_id: str) -> JSONResponse | dict[str, Any]:
        """'Sync now' from the web UI: the client's app starts the sync (at once while it is open and
        listening, else when it next asks); nothing is prepared on the server before that."""
        if await app.clients.get(device_id) is None:
            return JSONResponse(
                {"error": "not_found", "message": "no such client"}, status_code=404
            )
        app.sync.request_client_sync(device_id)
        return {"requested": True}

    @r.delete("/{device_id}/outbox", response_model=None)
    async def clear_outbox(device_id: str) -> JSONResponse | dict[str, Any]:
        """Drop what waits for the client on the server's disk (not while it syncs)."""
        if app.client_sessions.open_for(device_id) is not None or app.sync.ctx.jobs.device_busy(
            device_id
        ):
            return JSONResponse(
                {"error": "busy", "message": "the client is syncing"}, status_code=409
            )
        dev = await app.client_sessions.device(device_id)
        freed = await asyncio.to_thread(dev.clear_outbox)
        app.bus.publish("client.changed", device_id=device_id)
        return {"freed": freed}

    @r.delete("/{device_id}/sync", response_model=None)
    async def cancel_request(device_id: str) -> dict[str, Any]:
        """Withdraw a 'Sync now' request and stop the client's running sync (its next ack answers `cancel`)."""
        asked = app.sync.cancel_client_sync_request(device_id)
        return {"cancelled": app.client_sessions.cancel(device_id) or asked}

    @r.get("/{device_id}/diagnostics", response_model=None)
    async def client_diagnostics(device_id: str) -> JSONResponse:
        """The client's last diagnostics upload, as a file to save."""
        doc = await asyncio.to_thread(app.client_sessions.diagnostics, device_id)
        if doc is None:
            return JSONResponse(
                {"error": "not_found", "message": "no diagnostics from this client"},
                status_code=404,
            )
        return JSONResponse(
            doc,
            headers={
                "Content-Disposition": f'attachment; filename="client-{device_id}-diagnostics.json"'
            },
        )

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
